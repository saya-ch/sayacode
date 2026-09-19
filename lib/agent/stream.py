"""流事件抽取归属 lib.agent 包，由 stream 模块承载。

从 SAIAgent 拆出：纯抽取逻辑，不懂 turn、不碰 runner。跨轮去重状态
（messages 通道是否已吐过正文）收拢在 AgentStreamExtractor 实例里，
每轮由调用方 reset()。

本模块只出结构化事件（StreamEvent）：字符串便捷包装
（增量元组 / 纯文本抽取 / 快照归一化）已删除，调用方直接消费事件。
快照归一化唯一实现在 lib.agent_recovery.coerce_stream_delta。
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ..core.agent_runtime import content_to_text, extract_tool_names, message_kind
from ..models.compat import extract_reasoning_text

# LangGraph 多模式流（stream_mode=[...]）会把每项包成 (mode, payload)。
# 已知模式名，用来把带模式标签的元组与恰好两个元素的普通元组区分开。
_LANGGRAPH_STREAM_MODES = frozenset(
    {"updates", "values", "messages", "custom", "debug", "tasks", "checkpoints"}
)


class AgentStreamExtractor:
    """流事件抽取器：一轮一个实例（或调 reset 复用）。"""

    def __init__(self) -> None:
        """初始化抽取器，并复位本轮去重状态。"""
        # 本轮是否已从 messages 模式拿到逐 token 增量。拿到之后，updates。
        # 模式里同一条 AI 消息的正文就不再发一次，否则整段回答会出现两遍。
        self.tokens_seen = False

    def reset(self) -> None:
        """重置跨轮去重状态（新 turn 开始时调用）。"""
        self.tokens_seen = False

    @staticmethod
    def split_mode_event(chunk: Any) -> tuple[Optional[str], Any]:
        """把 (mode, payload) 拆开；不是多模式事件就返回 (None, chunk)。"""
        if (
            isinstance(chunk, tuple)
            and len(chunk) == 2
            and isinstance(chunk[0], str)
            and chunk[0] in _LANGGRAPH_STREAM_MODES
        ):
            return chunk[0], chunk[1]
        return None, chunk

    @staticmethod
    def format_tool_call_label(tool_names: list[str]) -> str:
        """工具名列表拼展示标签（重复折叠为 xN）。"""
        if len(tool_names) == 1:
            return tool_names[0]
        from collections import Counter
        counts = Counter(tool_names)
        return ", ".join(f"{name} x{n}" if n > 1 else name for name, n in counts.items())

    def extract_token_event(self, message: Any) -> Any:
        """messages 模式下的逐 token 增量转结构化事件（推理优先，其次正文）。"""
        from ..runtime.events import StreamEvent

        kind = message_kind(message)
        if isinstance(message, ToolMessage) or kind == "tool":
            return StreamEvent(kind="text", text="")
        if isinstance(message, (HumanMessage, SystemMessage)) or kind in {"human", "user", "system"}:
            return StreamEvent(kind="text", text="")
        extra = getattr(message, "additional_kwargs", None) or {}
        reasoning = extract_reasoning_text(extra)
        if reasoning:
            return StreamEvent.reasoning(reasoning)
        content = content_to_text(getattr(message, "content", ""))
        if content:
            return StreamEvent.text_delta(content)
        return StreamEvent(kind="text", text="")

    def extract_message_event(self, msg: Any) -> Any:
        """单条消息转结构化事件（工具调用 / 正文 / 空）。"""
        from ..runtime.events import StreamEvent

        kind = message_kind(msg)
        if isinstance(msg, ToolMessage) or kind == "tool":
            return self.extract_tool_event(msg)
        if isinstance(msg, (HumanMessage, SystemMessage)) or kind in {"human", "user", "system"}:
            return StreamEvent(kind="text", text="")
        is_ai_message = isinstance(msg, AIMessage) or kind in {"ai", "assistant", "aimessagechunk"}
        if is_ai_message:
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                tool_names = extract_tool_names(tool_calls)
                label = self.format_tool_call_label(tool_names)
                return StreamEvent.tool_start(label)
            if self.tokens_seen:
                return StreamEvent(kind="text", text="")
            content = content_to_text(getattr(msg, "content", ""))
            if content:
                return StreamEvent.text_delta(content)
            return StreamEvent(kind="text", text="")
        if isinstance(msg, str) and msg:
            return StreamEvent.text_delta(msg)
        return StreamEvent(kind="text", text="")

    def extract_tool_event(self, msg: Any) -> Any:
        """ToolMessage 转结构化事件（tool_result / tool_error）。"""
        from ..runtime.events import StreamEvent

        if hasattr(msg, "content"):
            content = content_to_text(getattr(msg, "content", ""))
            tool_name = getattr(msg, "name", "") or ""
            if not tool_name:
                tool_name = getattr(msg, "tool_call_id", "") or "tool"
            if content.startswith("工具执行失败") or content.startswith("❌") or content.startswith("⚠️"):
                return StreamEvent.tool_error(tool_name, content)
            if len(content) > 200:
                content = content[:200] + "..."
            return StreamEvent.tool_result(tool_name, content)
        return StreamEvent(kind="text", text="")

    def extract_stream_delta(self, chunk: Any) -> Any:
        """从 Agent 流式事件中提取结构化事件（多模式/字典/元组/单消息全覆盖）。"""
        mode, payload = self.split_mode_event(chunk)
        if mode == "messages":
            self.tokens_seen = True
            message = payload[0] if isinstance(payload, tuple) and payload else payload
            return self.extract_token_event(message)
        if mode is not None:
            return self.extract_stream_delta(payload)
        if isinstance(chunk, dict):
            if "agent" in chunk:
                agent_data = chunk["agent"]
                if isinstance(agent_data, dict) and "messages" in agent_data:
                    msgs = agent_data["messages"]
                    if msgs:
                        return self.extract_message_event(msgs[-1])
                if isinstance(agent_data, list) and agent_data:
                    return self.extract_message_event(agent_data[-1])
            if "tools" in chunk:
                tools_data = chunk["tools"]
                if isinstance(tools_data, dict) and "messages" in tools_data:
                    msgs = tools_data["messages"]
                    if msgs:
                        return self.extract_tool_event(msgs[-1])
                if isinstance(tools_data, list) and tools_data:
                    return self.extract_tool_event(tools_data[-1])
            if "messages" in chunk:
                msgs = chunk["messages"]
                if msgs:
                    return self.extract_message_event(msgs[-1])
            _visited = {"agent", "tools", "messages"}
            for key, value in chunk.items():
                if key in _visited:
                    continue
                event = self.extract_stream_delta(value)
                if event is not None and (event.display_text or event.kind in {"tool_start", "tool_result", "tool_error"}):
                    return event
            return None
        if isinstance(chunk, tuple):
            for item in chunk:
                event = self.extract_stream_delta(item)
                if event is not None and (event.display_text or event.kind in {"tool_start", "tool_result", "tool_error"}):
                    return event
            return None
        return self.extract_message_event(chunk)

__all__ = ["AgentStreamExtractor"]
