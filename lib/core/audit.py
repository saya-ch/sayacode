"""SAYACODE 运行事件的持久化本地审计日志。

负责审计事件脱敏、追加写入与按条件回读。
核心类：AuditEvent、AuditLogService；函数：append_audit_event。
调用链：hook／middleware→append_audit_event→AuditLogService。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4
import csv
import io
import json

from .paths import SayacodePaths
from .private_io import ensure_private_dir, restrict_permissions
from ..i18n import tr


SENSITIVE_KEY_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
MAX_AUDIT_FIELD = 2000


@dataclass(frozen=True)
class AuditEvent:
    """一条已脱敏的运行审计事件。"""

    event_type: str
    action: str
    workspace: str = ""
    actor: str = "local"
    allowed: Optional[bool] = None
    trace_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """转为已脱敏的可序列化字典。"""
        data = {
            "timestamp": self.timestamp,
            "type": self.event_type,
            "action": self.action,
            "actor": self.actor,
            "workspace": self.workspace,
            "trace_id": self.trace_id,
            "details": redact_value(self.details),
        }
        if self.allowed is not None:
            data["allowed"] = self.allowed
        return data


class AuditLogService:
    """只追加的 JSONL 审计日志，支持脱敏与容错读取。"""

    def __init__(self, path: Optional[str | Path] = None, paths: Optional[SayacodePaths] = None) -> None:
        self.paths = paths or SayacodePaths.resolve(create=True)
        self.path = Path(path).expanduser() if path else self.paths.audit_log

    def append(self, event: AuditEvent | Dict[str, Any]) -> Path:
        """追加一条审计事件并返回日志路径。"""
        record = event.to_dict() if isinstance(event, AuditEvent) else redact_value(event)
        ensure_private_dir(self.path.parent)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        restrict_permissions(self.path, directory=False)
        return self.path

    def _load_events(self) -> list[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            print(tr("core.read_failed", error=str(e)))
            return []
        events: list[Dict[str, Any]] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except Exception:
                # 静默忽略：审计日志中可能存在格式错误的行
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def read_recent(self, limit: int = 50) -> list[Dict[str, Any]]:
        """读取最近若干条审计事件。"""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            print(tr("core.read_failed", error=str(e)))
            return []

        events: list[Dict[str, Any]] = []
        for line in lines[-max(1, int(limit or 1)) * 3:]:
            try:
                payload = json.loads(line)
            except Exception:
                # 静默忽略：审计日志中可能存在格式错误的行
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events[-max(1, int(limit or 1)):]

    def read_by_trace(self, trace_id: str, limit: int = 500) -> list[Dict[str, Any]]:
        """按 trace_id 过滤并返回事件。"""
        if not trace_id:
            return []
        events = self._load_events()
        matched = [e for e in events if e.get("trace_id") == trace_id]
        return matched[-max(1, int(limit or 1)):]

    def list_recent_traces(self, limit: int = 10) -> list[Dict[str, Any]]:
        """按 trace_id 分组并返回最近追踪摘要。"""
        events = self._load_events()
        groups: Dict[str, list[Dict[str, Any]]] = {}
        order: list[str] = []
        for e in events:
            tid = str(e.get("trace_id") or "")
            if not tid:
                continue
            if tid not in groups:
                groups[tid] = []
                order.append(tid)
            groups[tid].append(e)
        selected = order[-max(1, int(limit or 1)):] if order else []
        result: list[Dict[str, Any]] = []
        for tid in selected:
            evts = groups[tid]
            tools = sorted({str(x.get("action") or "") for x in evts if x.get("type") == "tool" and x.get("action")})
            failed = any(x.get("allowed") is False for x in evts)
            result.append({"trace_id": tid, "events": len(evts), "tools": tools, "failed": failed})
        return result

    def read_by_type(self, event_type: str, limit: int = 50) -> list[Dict[str, Any]]:
        """按事件类型过滤并返回最近记录。"""
        events = self._load_events()
        matched = [e for e in events if e.get("type") == event_type]
        return matched[-max(1, int(limit or 1)):]

    def read_by_workspace(self, workspace: str, limit: int = 50) -> list[Dict[str, Any]]:
        """按工作区过滤并返回最近记录。"""
        events = self._load_events()
        matched = [e for e in events if e.get("workspace") == workspace]
        return matched[-max(1, int(limit or 1)):]

    def read_by_timerange(self, start: str, end: str, limit: int = 100) -> list[Dict[str, Any]]:
        """按时间区间过滤并返回最近记录。"""
        events = self._load_events()
        matched = [e for e in events if start <= e.get("timestamp", "") <= end]
        return matched[-max(1, int(limit or 1)):]

    def apply_retention(self, max_days: int = 90, max_entries: int = 10000) -> int:
        """按保留策略裁剪过期事件并返回删除数。"""
        if not self.path.exists():
            return 0
        events = self._load_events()
        if not events:
            return 0

        cutoff_ts = datetime.now(timezone.utc).timestamp() - max_days * 86400
        kept = []
        for e in events:
            try:
                ts = datetime.fromisoformat(e.get("timestamp", "")).timestamp()
            except Exception:
                # 静默忽略：事件时间戳格式异常，按过期处理
                ts = 0
            if ts >= cutoff_ts:
                kept.append(e)

        if len(kept) > max_entries:
            kept.sort(key=lambda e: e.get("timestamp", ""))
            kept = kept[-max_entries:]

        removed = len(events) - len(kept)
        if removed == 0:
            return 0

        ensure_private_dir(self.path.parent)
        with self.path.open("w", encoding="utf-8") as handle:
            for e in kept:
                handle.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        restrict_permissions(self.path, directory=False)
        return removed

    def export(self, fmt: str = "jsonl") -> str:
        """导出全部审计事件为文本。"""
        events = self._load_events()
        if fmt == "csv":
            buf = io.StringIO()
            fieldnames = ["timestamp", "type", "action", "actor", "workspace", "allowed", "trace_id"]
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for e in events:
                row = {}
                for k in fieldnames:
                    v = e.get(k, "")
                    if isinstance(v, bool):
                        v = str(v).lower()
                    row[k] = v
                writer.writerow(row)
            return buf.getvalue()
        lines = [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in events]
        return "\n".join(lines) + ("\n" if lines else "")


def redact_value(value: Any, key: str = "") -> Any:
    """返回一个对密钥脱敏、对超长字段截断的 JSON 安全值。"""
    if _is_sensitive_key(key):
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return "***"

    if isinstance(value, dict):
        return {
            str(item_key): redact_value(item_value, str(item_key))
            for item_key, item_value in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item, key) for item in list(value)[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if not isinstance(value, str):
            return value
        if len(value) <= MAX_AUDIT_FIELD:
            return value
        return value[:MAX_AUDIT_FIELD] + "...[truncated]"
    text = str(value)
    return text[:MAX_AUDIT_FIELD] + ("...[truncated]" if len(text) > MAX_AUDIT_FIELD else "")


def _is_sensitive_key(key: str) -> bool:
    normalized = str(key or "").upper()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def append_audit_event(
    event_type: str,
    action: str,
    *,
    workspace: str | Path | None = None,
    allowed: Optional[bool] = None,
    details: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    service: Optional[AuditLogService] = None,
) -> None:
    """尽力而为的辅助函数，供不应因审计 I/O 失败而中断的运行时服务使用。"""
    try:
        # 调用树已删除（走 LangSmith）：无显式 trace_id 即 mint 新的，不再回查上下文。
        tid = str(trace_id or "") or str(uuid4())
        payload = dict(details or {})
        (service or AuditLogService()).append(
            AuditEvent(
                event_type=event_type,
                action=action,
                workspace=str(workspace or ""),
                allowed=allowed,
                details=payload,
                trace_id=tid,
            )
        )
    except Exception as e:
        print(tr("core.audit_event_failed", error=str(e)))
        return


def resolve_tool_workspace() -> str:
    """解析当前工具工作区：文件 → Shell → Git → 项目，取首个非空。

    唯一入口：``lib/tools/__init__.py`` 的 hook 包裹与
    ``lib/core/middleware.py`` 的图中间件此前各拼一遍四元 ``or``，
    在此收敛。惰性导入避免 ``core ↔ tools`` 循环。
    """
    try:
        from ..tools import (
            get_file_tools_workspace,
            get_git_tools_workspace,
            get_project_tools_workspace,
            get_shell_tools_workspace,
        )
    except Exception:
        return ""
    try:
        return str(
            get_file_tools_workspace()
            or get_shell_tools_workspace()
            or get_git_tools_workspace()
            or get_project_tools_workspace()
            or ""
        )
    except Exception:
        return ""


def audit_tool_event(
    tool_name: str,
    arguments: Any,
    *,
    allowed: bool,
    error: str = "",
    exception_type: str = "",
    result_preview: str = "",
    artifact: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
) -> None:
    """写一条工具审计事件：工作区解析 + artifact 契约校验内聚一处。

    ``artifact`` 非空时先做契约校验（告警不阻断），再随 ``details`` 落盘。
    """
    details: Dict[str, Any] = {"arguments": arguments}
    if error:
        details["error"] = error
    if exception_type:
        details["exception_type"] = exception_type
    if result_preview:
        details["result_preview"] = result_preview
    if artifact is not None:
        try:
            from .tool_result import validate_tool_artifact
            import logging

            problems = validate_tool_artifact(artifact)
            if problems:
                logging.getLogger(__name__).warning("artifact 不合契约: %s", problems)
        except Exception:
            pass
        details["artifact"] = artifact
    append_audit_event(
        "tool", tool_name, workspace=resolve_tool_workspace(), allowed=allowed, details=details,
        trace_id=trace_id,
    )


def read_recent_audit_events(limit: int = 50) -> list[Dict[str, Any]]:
    """读取默认审计日志的最近事件。"""
    return AuditLogService().read_recent(limit=limit)


__all__ = [
    "AuditEvent",
    "AuditLogService",
    "append_audit_event",
    "audit_tool_event",
    "read_recent_audit_events",
    "redact_value",
    "resolve_tool_workspace",
]
