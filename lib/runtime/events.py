"""Public, machine-readable events for headless Agent execution."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, TextIO
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from ..core.agent_runtime import content_to_text, message_kind
from ..core.audit import redact_value


HEADLESS_EVENT_SCHEMA_VERSION = 1
_RESERVED_EVENT_FIELDS = {"schema_version", "sequence", "type", "run_id", "timestamp"}
_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password|authorization|credential)"
        r"(\s*[:=]\s*)[\"']?[^\s\"',;}]+"
    ),
)


class JsonlEventWriter:
    """Write one versioned, flushed JSON object per line."""

    def __init__(self, stream: TextIO, *, run_id: str | None = None) -> None:
        self.stream = stream
        self.run_id = run_id or str(uuid4())
        self.sequence = 0

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        self.sequence += 1
        event_payload = _sanitize_event_payload(event_type, payload)
        for field in _RESERVED_EVENT_FIELDS:
            event_payload.pop(field, None)
        event = {
            "schema_version": HEADLESS_EVENT_SCHEMA_VERSION,
            "sequence": self.sequence,
            "type": event_type,
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event_payload,
        }
        self.stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self.stream.flush()
        return event


def extract_public_tool_events(chunk: Any) -> list[dict[str, Any]]:
    """Extract tool lifecycle events without exposing model reasoning fields."""
    messages: list[Any] = []
    _collect_messages(chunk, messages, seen=set())
    events: list[dict[str, Any]] = []
    for message in messages:
        kind = message_kind(message)
        if isinstance(message, ToolMessage) or kind == "tool":
            events.append(_tool_result_event(message))
            continue
        is_ai = isinstance(message, AIMessage) or kind in {"ai", "assistant", "aimessagechunk"}
        if not is_ai:
            continue
        tool_calls = _message_value(message, "tool_calls", []) or []
        if not tool_calls:
            additional_kwargs = _message_value(message, "additional_kwargs", {}) or {}
            tool_calls = _message_value(additional_kwargs, "tool_calls", []) or []
        for call in tool_calls:
            event = _tool_call_event(call)
            if event is not None:
                events.append(event)
    return events


def public_event_identity(event: dict[str, Any]) -> str:
    """Return a stable key used to suppress replayed LangGraph value snapshots."""
    event_type = str(event.get("type") or "")
    call_id = str(event.get("tool_call_id") or "")
    if call_id:
        return f"{event_type}:{call_id}"
    # Calls without provider IDs cannot be safely deduplicated: two identical
    # invocations may be intentional. LangGraph snapshots with IDs still get
    # replay suppression through the branch above.
    return ""


def _collect_messages(value: Any, output: list[Any], seen: set[int]) -> None:
    if value is None:
        return
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)

    kind = message_kind(value)
    if isinstance(value, (AIMessage, ToolMessage)) or kind in {
        "ai",
        "assistant",
        "aimessagechunk",
        "tool",
    }:
        output.append(value)
        return

    if isinstance(value, dict):
        serialized_type = str(value.get("type") or "").lower()
        if serialized_type in {"ai", "assistant", "aimessagechunk", "tool"}:
            output.append(value)
            return
        for key in ("agent", "model", "tools", "messages"):
            if key in value:
                _collect_messages(value[key], output, seen)
        return

    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_messages(item, output, seen)


def _tool_call_event(call: Any) -> dict[str, Any] | None:
    name = _message_value(call, "name", "")
    arguments = _message_value(call, "args", None)
    call_id = _message_value(call, "id", "") or _message_value(call, "tool_call_id", "")
    function = _message_value(call, "function", None)
    if function is not None:
        name = name or _message_value(function, "name", "")
        if arguments is None:
            arguments = _message_value(function, "arguments", None)
    if not name:
        return None
    return {
        "type": "tool.started",
        "tool_name": str(name),
        "tool_call_id": str(call_id or ""),
        "arguments": _redact_public_value(_normalize_arguments(arguments)),
    }


def _tool_result_event(message: Any) -> dict[str, Any]:
    content = content_to_text(_message_value(message, "content", ""))
    tool_name = _message_value(message, "name", "") or "tool"
    call_id = _message_value(message, "tool_call_id", "")
    status = str(_message_value(message, "status", "") or "").lower()
    is_error = status == "error" or content.startswith(("工具执行失败", "❌", "⚠️"))
    return {
        "type": "tool.completed",
        "tool_name": str(tool_name),
        "tool_call_id": str(call_id or ""),
        "result": _redact_public_value(_normalize_result(content)),
        "is_error": is_error,
    }


def _message_value(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalize_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            return parsed
        except json.JSONDecodeError:
            return {"raw": arguments}
    if arguments is None:
        return {}
    return arguments


def _normalize_result(result: str) -> Any:
    stripped = result.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return result


def _redact_public_value(value: Any) -> Any:
    redacted = redact_value(value)
    if isinstance(redacted, dict):
        return {key: _redact_public_value(item) for key, item in redacted.items()}
    if isinstance(redacted, list):
        return [_redact_public_value(item) for item in redacted]
    if not isinstance(redacted, str):
        return redacted

    text = redacted
    text = _INLINE_SECRET_PATTERNS[0].sub(r"\1***", text)
    text = _INLINE_SECRET_PATTERNS[1].sub("sk-***", text)
    text = _INLINE_SECRET_PATTERNS[2].sub(r"\1\2***", text)
    return text


def _sanitize_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = _redact_public_value(dict(payload))
    if event_type == "tool.started":
        sanitized["arguments"] = _redact_public_value(sanitized.get("arguments") or {})
    elif event_type == "tool.completed":
        sanitized["result"] = _redact_public_value(sanitized.get("result") or "")
    return sanitized


__all__ = [
    "HEADLESS_EVENT_SCHEMA_VERSION",
    "JsonlEventWriter",
    "extract_public_tool_events",
    "public_event_identity",
]
