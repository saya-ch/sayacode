"""终端公开事件、退出码与脱敏。"""

from __future__ import annotations

import json
import re
from typing import Any, TextIO
from uuid import uuid4

EVENT_SCHEMA_VERSION = 1

_PRIVATE_KEYS = {
    "reasoning",
    "reasoning_content",
    "thinking",
    "chain_of_thought",
    "hidden",
    "private",
    "raw_response",
}

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|credential|authorization|^token$|access[_-]?token|refresh[_-]?token|id[_-]?token)$",
    re.I,
)

_BEARER = re.compile(r"\bBearer\s+[^\s,;]+", re.I)

_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")

_TOOL_EVENT_FIELDS = {
    "tool_name",
    "tool_call_id",
    "is_error",
    "duration_ms",
    "outcome",
    "error_type",
}


def _redact(value: Any, *, key: str = "") -> Any:
    """按键名脱敏事件载荷，挡住密钥与思考过程。
    参数是待处理值与所在键名，返回脱敏后的值。
    私有键直接置空，密钥类键给星号，令牌格式按正则替换。"""
    if key.lower() in _PRIVATE_KEYS:
        return None
    if _SECRET_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        return {
            str(k): _redact(v, key=str(k))
            for k, v in value.items()
            if str(k).lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _OPENAI_KEY.sub("***", _BEARER.sub("Bearer ***", value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    """把内部事件裁成可对外输出的公开形状。
    参数是内部事件字典，返回脱敏后的公开事件。
    工具事件只保留白名单字段，其余事件去掉序号类元字段。"""
    kind = str(event.get("type") or "graph.event")
    if kind == "event":
        return {"type": "graph.event", "method": str(event.get("method") or "")}
    if kind.startswith("tool."):
        return {
            "type": kind,
            **{key: _redact(event[key], key=key) for key in _TOOL_EVENT_FIELDS if key in event},
        }
    return {
        "type": kind,
        **{
            str(k): _redact(v, key=str(k))
            for k, v in event.items()
            if k not in {"type", "schema_version", "sequence", "run_id"}
            and str(k).lower() not in _PRIVATE_KEYS
        },
    }


class JsonlWriter:
    """无头模式逐行输出事件的写器，自动补序号与版本。
    参数是输出流与运行号，缺省自动生成运行号。
    约束是每行一个合法事件，调用方按流式顺序调用。"""
    def __init__(self, stream: TextIO, *, run_id: str | None = None) -> None:
        self.stream = stream
        self.run_id = run_id or uuid4().hex
        self.sequence = 0

    def emit(self, event: dict[str, Any]) -> None:
        """输出单个公开事件并立即刷盘，保证管道实时可见。
        参数是内部事件字典，返回无。
        内部先脱敏再套序号与运行号信封，失败不重试。"""
        self.sequence += 1
        public = _public_event(event)
        envelope = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": self.sequence,
            "run_id": self.run_id,
            **public,
        }
        self.stream.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
        self.stream.flush()


def _run_ok(result: Any) -> bool:
    """判断运行结果是否算成功，供退出码与展示共用。
    参数是运行结果，字典看状态与成功标记，非字典按成功处理。
    暂停中断等状态都算未成功，调用方据此选提示语。"""
    return not isinstance(result, dict) or (
        result.get("ok", True) is not False
        and result.get("status") not in {"failed", "error", "paused", "interrupted", "stopped"}
    )


def _exit_code(result: Any) -> int:
    """把运行结果映射为进程退出码，供无头模式返回。
    参数是运行结果，返回整数退出码。
    暂停中断给三，成功给零，失败给一，脚本靠它分支。"""
    if isinstance(result, dict) and result.get("status") in {"paused", "interrupted"}:
        return 3
    return 0 if _run_ok(result) else 1


def _terminal_type(result: Any) -> str:
    """按退出码选出终止事件的类型名。
    参数是运行结果，返回完成暂停或失败三者之一。
    只看退出码映射，不解析具体错误文本。"""
    code = _exit_code(result)
    return "run.paused" if code == 3 else "run.completed" if code == 0 else "run.failed"


def _with_task_outcome(result: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """把后台任务结局合并进主结果，决定最终状态与提示。
    参数是主结果与后台任务列表，返回合并后的新字典。
    无任务直接返回原结果，有暂停或失败会改写状态与错误文案。
    流程分三段，先拼接父轮次唤醒文本，再看暂停与失败两类坏结局，最后收尾未完成的任务。
    坑点是原字典不改动，返回的是合并后的副本。"""
    if not tasks:
        return result
    merged = {**result, "tasks": tasks}
    wakes = [task["parent_wake"] for task in tasks if isinstance(task.get("parent_wake"), dict)]
    if wakes:
        merged["parent_wakes"] = wakes
        responses = [str(wake.get("response") or "") for wake in wakes]
        parts = [str(result.get("response") or ""), *responses]
        merged["response"] = "\n\n".join(part for part in parts if part)
    if not _run_ok(merged):
        return merged
    paused = [task for task in tasks if task.get("status") in {"paused", "interrupted"}]
    paused.extend(wake for wake in wakes if wake.get("type") == "agent.wake.paused")
    failed = [task for task in tasks if task.get("status") in {"failed", "error", "stopped"}]
    failed.extend(
        wake for wake in wakes if wake.get("type") in {"agent.wake.failed", "agent.wake.stopped"}
    )
    if paused:
        merged.update(ok=False, status="paused", error="Background task requires attention")
    elif failed:
        merged.update(ok=False, status="failed", error="Background task failed or stopped")
    elif any(task.get("status") not in {"idle"} for task in tasks):
        merged.update(ok=False, status="failed", error="Background task did not finish")
    return merged


def _response_text(result: Any) -> str:
    """取出结果里可直接打印的回答文本。
    参数是运行结果，返回回答或文本字段的字符串。
    字典优先取回答字段，非字典按原样转字符串。"""
    if isinstance(result, dict):
        return str(result.get("response") or result.get("text") or "")
    return str(result or "")
