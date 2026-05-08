"""Durable local audit log for SAYACODE runtime events."""

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
    """One redacted runtime audit event."""

    event_type: str
    action: str
    workspace: str = ""
    actor: str = "local"
    allowed: Optional[bool] = None
    trace_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
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
    """Append-only JSONL audit log with redaction and tolerant reads."""

    def __init__(self, path: Optional[str | Path] = None, paths: Optional[SayacodePaths] = None) -> None:
        self.paths = paths or SayacodePaths.resolve(create=True)
        self.path = Path(path).expanduser() if path else self.paths.audit_log

    def append(self, event: AuditEvent | Dict[str, Any]) -> Path:
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

    def read_by_type(self, event_type: str, limit: int = 50) -> list[Dict[str, Any]]:
        events = self._load_events()
        matched = [e for e in events if e.get("type") == event_type]
        return matched[-max(1, int(limit or 1)):]

    def read_by_workspace(self, workspace: str, limit: int = 50) -> list[Dict[str, Any]]:
        events = self._load_events()
        matched = [e for e in events if e.get("workspace") == workspace]
        return matched[-max(1, int(limit or 1)):]

    def read_by_timerange(self, start: str, end: str, limit: int = 100) -> list[Dict[str, Any]]:
        events = self._load_events()
        matched = [e for e in events if start <= e.get("timestamp", "") <= end]
        return matched[-max(1, int(limit or 1)):]

    def apply_retention(self, max_days: int = 90, max_entries: int = 10000) -> int:
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
    """Return a JSON-safe value with secrets and oversized fields redacted."""
    if _is_sensitive_key(key):
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
    service: Optional[AuditLogService] = None,
) -> None:
    """Best-effort helper for runtime services that should not fail on audit I/O."""
    try:
        (service or AuditLogService()).append(
            AuditEvent(
                event_type=event_type,
                action=action,
                workspace=str(workspace or ""),
                allowed=allowed,
                details=details or {},
                trace_id=str(uuid4()),
            )
        )
    except Exception as e:
        print(tr("core.audit_event_failed", error=str(e)))
        return


def read_recent_audit_events(limit: int = 50) -> list[Dict[str, Any]]:
    return AuditLogService().read_recent(limit=limit)


__all__ = [
    "AuditEvent",
    "AuditLogService",
    "append_audit_event",
    "read_recent_audit_events",
    "redact_value",
]
