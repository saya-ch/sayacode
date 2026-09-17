"""用于 headless Agent 执行的公开、机器可读事件。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Optional, TextIO
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from ..core.agent_runtime import content_to_text, message_kind
from ..core.audit import redact_value


# ==============================================================================
# 结构化流事件 —— 取代 [思考:]/[调用工具:] 带内字符串协议
# ==============================================================================


@dataclass(frozen=True)
class StreamEvent:
    """一次流输出的结构化事件。

    取代 ``[思考: ...]`` / ``[调用工具: ...]`` / ``[工具结果: ...]`` 这类带内
    字符串协议：agent 层只发射事件，theme 层只消费事件，两边不共享任何
    字符串格式约定。``text`` 非空时是正文增量；``reasoning`` 非空时是思考
    增量（两者互斥，由发射方保证）。
    """

    kind: str  # "text" | "reasoning" | "tool_start" | "tool_result" | "tool_error"
    text: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    preview: str = ""
    is_error: bool = False

    @classmethod
    def text_delta(cls, text: str) -> "StreamEvent":
        return cls(kind="text", text=text)

    @classmethod
    def reasoning(cls, text: str) -> "StreamEvent":
        return cls(kind="reasoning", text=text)

    @classmethod
    def tool_start(cls, name: str, call_id: str = "") -> "StreamEvent":
        return cls(kind="tool_start", tool_name=name, tool_call_id=call_id)

    @classmethod
    def tool_result(cls, name: str, preview: str, call_id: str = "", is_error: bool = False) -> "StreamEvent":
        return cls(kind="tool_result", tool_name=name, preview=preview, tool_call_id=call_id, is_error=is_error)

    @classmethod
    def tool_error(cls, name: str, preview: str, call_id: str = "") -> "StreamEvent":
        return cls(kind="tool_error", tool_name=name, preview=preview, tool_call_id=call_id, is_error=True)

    @property
    def display_text(self) -> str:
        """给 theme 渲染用的纯文本（不含结构化字段）。

        与旧字符串协议完全一致：reasoning → ``[思考: ...]``，tool_start →
        ``[调用工具: ...]``，tool_result → ``[工具结果: ...]``，tool_error →
        ``[工具执行出错: ...]``，text → 原文。
        """
        if self.kind == "reasoning":
            return f"[思考: {self.text}]"
        if self.kind == "tool_start":
            return f"[调用工具: {self.tool_name}]"
        if self.kind == "tool_result":
            return f"[工具结果: {self.tool_name} | {self.preview}]"
        if self.kind == "tool_error":
            return f"[工具执行出错: {self.tool_name} | {self.preview}]"
        return self.text


def event_from_legacy_marker(chunk: str) -> Optional[StreamEvent]:
    """把旧字符串标记解析成 StreamEvent（兼容层，给 theme 双签收用）。

    解析失败返回 None（不是标记，是普通文本）。
    """
    if not isinstance(chunk, str):
        return None
    text = chunk.strip()
    for prefix, kind in [
        ("[调用工具:", "tool_start"),
        ("[工具结果:", "tool_result"),
        ("[工具执行出错:", "tool_error"),
        ("[思考:", "reasoning"),
    ]:
        if text.startswith(prefix) and text.endswith("]"):
            inner = text[len(prefix):-1].strip()
            if kind == "reasoning":
                return StreamEvent(kind=kind, text=inner)
            if kind == "tool_start":
                return StreamEvent(kind=kind, tool_name=inner or "tool")
            # result / error: "工具名 | 内容"
            if " | " in inner:
                name, preview = inner.split(" | ", 1)
                return StreamEvent(kind=kind, tool_name=name.strip(), preview=preview.strip())
            return StreamEvent(kind=kind, tool_name="tool", preview=inner)
    return None


# ==============================================================================
# headless JSONL 事件写入
# ==============================================================================


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
    """每行写入一个带版本、已 flush 的 JSON 对象。"""

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
    """提取 tool 生命周期事件，且不暴露模型推理字段。"""
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
    """返回用于抑制重放的 LangGraph value 快照的稳定 key。"""
    event_type = str(event.get("type") or "")
    call_id = str(event.get("tool_call_id") or "")
    if call_id:
        return f"{event_type}:{call_id}"
    # 没有 provider ID 的调用无法安全去重：两次完全相同的
    # 调用可能是有意为之。带 ID 的 LangGraph 快照仍会通过
    # 上面的分支获得重放抑制。
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
    "StreamEvent",
    "event_from_legacy_marker",
    "extract_public_tool_events",
    "public_event_identity",
]
