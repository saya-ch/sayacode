"""原生事件流到公开事实的轻量映射。

只做投影转换，不改图状态，不存历史。
调用方按需消费助手增量和工具起止事件。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage


def _final_text(state: Any) -> str:
    """从最终状态倒推最后一段助手正文，跳过续写标记。"""
    values = getattr(state, "value", state)
    if isinstance(values, dict):
        messages = values.get("messages", [])
        parts: list[str] = []
        for message in reversed(messages):
            if isinstance(message, AIMessage) and not message.tool_calls:
                parts.append(message.text if hasattr(message, "text") else str(message.content))
                continue
            if (
                parts
                and isinstance(message, HumanMessage)
                and message.additional_kwargs.get("sayacode_continuation")
            ):
                continue
            if parts:
                return "".join(reversed(parts))
            if isinstance(message, dict) and message.get("type") in {"ai", "assistant"}:
                content = message.get("content", "")
                return content if isinstance(content, str) else str(content)
        if parts:
            return "".join(reversed(parts))
    return ""


def _message_text(message: Any) -> str:
    """取消息正文，兼容字符串和块列表两种形态。"""
    value = getattr(message, "content", message)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item) for item in value
        )
    return str(value or "")


def action_requests(interrupts: list[Any]) -> list[dict[str, Any]]:
    """从中断列表提取待审批的动作请求。

    参数是图返回的中断列表，返回纯字典动作表。
    坑点是非字典载荷直接丢弃，调用方无需再判结构。"""
    actions: list[dict[str, Any]] = []
    for interrupt in interrupts or []:
        value = getattr(interrupt, "value", interrupt)
        if isinstance(value, dict):
            candidate = value.get("action_requests", [])
            if isinstance(candidate, list):
                actions.extend(item for item in candidate if isinstance(item, dict))
    return actions


class EventProjector:
    """仅保存流式分块所需的瞬时名称与角色。

    用线程加运行标识区分并发流，避免串台。
    结束或失败后及时清理，不长期持有状态。"""

    def __init__(self) -> None:
        self._stream_roles: dict[tuple[str, str], str] = {}
        self._stream_tool_names: dict[tuple[str, str], str] = {}

    def normalize(self, event: dict[str, Any], thread_id: str) -> list[dict[str, Any]]:
        """映射原生信封到精简公开事件协议。

        参数是原生事件字典和线程标识，返回零到多个公开事件。
        调用约束是有状态，同一实例应串行喂同一线程的事件。
        坑点是只透出助手增量和工具起止，未知方法直接返回空。"""
        # 先看消息通道，助手增量才透出，其他角色只维护瞬时状态。
        # 再看工具通道，开始和结束分别映射，错误统一收敛为失败事件。
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        data = params.get("data") if isinstance(params, dict) else None
        if method == "messages":
            payload, metadata = (
                (data[0], data[1])
                if isinstance(data, (list, tuple)) and len(data) == 2
                else (data, {})
            )
            if isinstance(payload, AIMessage):
                text = _message_text(payload)
                return (
                    [{"type": "assistant.delta", "thread_id": thread_id, "delta": text}]
                    if text
                    else []
                )
            if not isinstance(payload, dict):
                return []
            run_id = str(metadata.get("run_id") or "") if isinstance(metadata, dict) else ""
            key = (thread_id, run_id)
            kind = payload.get("event")
            if kind == "message-start":
                self._stream_roles[key] = str(payload.get("role") or "")
            elif kind == "message-finish":
                self._stream_roles.pop(key, None)
            elif kind == "content-block-delta" and self._stream_roles.get(key) in {
                "ai",
                "assistant",
            }:
                delta = payload.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text-delta":
                    delta_text = delta.get("text")
                    if isinstance(delta_text, str) and delta_text:
                        return [
                            {"type": "assistant.delta", "thread_id": thread_id, "delta": delta_text}
                        ]
        if method == "tools":
            payload = data if isinstance(data, dict) else {}
            kind = payload.get("event")
            call_id = str(payload.get("tool_call_id") or "")
            key = (thread_id, call_id)
            if kind == "tool-started":
                name = str(payload.get("tool_name") or "tool")
                self._stream_tool_names[key] = name
                return [
                    {
                        "type": "tool.started",
                        "thread_id": thread_id,
                        "tool_name": name,
                        "tool_call_id": call_id,
                        "tool_input": payload.get("input", {}),
                    }
                ]
            if kind not in {"tool-error", "tool-finished"}:
                return []
            name = self._stream_tool_names.pop(key, str(payload.get("tool_name") or "tool"))
            if kind == "tool-error":
                return [
                    {
                        "type": "tool.failed",
                        "thread_id": thread_id,
                        "tool_name": name,
                        "tool_call_id": call_id,
                        "error": str(payload.get("message") or "Tool failed"),
                    }
                ]
            if kind == "tool-finished":
                output = payload.get("output")
                if getattr(output, "status", None) == "error":
                    return [
                        {
                            "type": "tool.failed",
                            "thread_id": thread_id,
                            "tool_name": name,
                            "tool_call_id": call_id,
                            "error": _message_text(output),
                        }
                    ]
                return [
                    {
                        "type": "tool.completed",
                        "thread_id": thread_id,
                        "tool_name": name,
                        "tool_call_id": call_id,
                        "tool_output": _message_text(output),
                    }
                ]
        return []
