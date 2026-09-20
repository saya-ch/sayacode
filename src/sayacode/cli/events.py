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
    def __init__(self, stream: TextIO, *, run_id: str | None = None) -> None:
        self.stream = stream
        self.run_id = run_id or uuid4().hex
        self.sequence = 0

    def emit(self, event: dict[str, Any]) -> None:
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
    return not isinstance(result, dict) or (
        result.get("ok", True) is not False
        and result.get("status") not in {"failed", "error", "paused", "interrupted", "stopped"}
    )


def _exit_code(result: Any) -> int:
    if isinstance(result, dict) and result.get("status") in {"paused", "interrupted"}:
        return 3
    return 0 if _run_ok(result) else 1


def _terminal_type(result: Any) -> str:
    code = _exit_code(result)
    return "run.paused" if code == 3 else "run.completed" if code == 0 else "run.failed"


def _with_task_outcome(result: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
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
    elif any(task.get("status") not in {"completed"} for task in tasks):
        merged.update(ok=False, status="failed", error="Background task did not finish")
    return merged


def _response_text(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("response") or result.get("text") or "")
    return str(result or "")
