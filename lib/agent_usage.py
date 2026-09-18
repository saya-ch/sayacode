"""Agent Token 用量统计：从模型结果中提取并上报用量。

从 SAIAgent 拆出：只读结果、只写模型用量口，不懂 turn、不碰 runner。
模型以构造入参传入（无 _record_usage 口则全部静默跳过）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from .core.agent_runtime import content_to_text


def safe_token_count(value: Any) -> int:
    """规范化可选或 provider 特有的 token 计数器，且不让 turn 失败。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def extract_from_message(msg: Any) -> Optional[Any]:
    """从单条消息的 usage_metadata/response_metadata 提 TokenUsage（无则 None）。"""
    from .models.base import TokenUsage

    usage_meta = None
    if hasattr(msg, "usage_metadata") and msg.usage_metadata:
        usage_meta = msg.usage_metadata
    elif hasattr(msg, "response_metadata") and msg.response_metadata:
        usage_meta = msg.response_metadata.get("token_usage") or msg.response_metadata.get("usage")
    if usage_meta:
        if isinstance(usage_meta, dict):
            usage = TokenUsage(
                prompt_tokens=safe_token_count(usage_meta.get("input_tokens", usage_meta.get("prompt_tokens", 0))),
                completion_tokens=safe_token_count(usage_meta.get("output_tokens", usage_meta.get("completion_tokens", 0))),
                total_tokens=safe_token_count(usage_meta.get("total_tokens", 0)),
            )
        else:
            usage = TokenUsage(
                prompt_tokens=safe_token_count(getattr(usage_meta, "input_tokens", getattr(usage_meta, "prompt_tokens", 0))),
                completion_tokens=safe_token_count(getattr(usage_meta, "output_tokens", getattr(usage_meta, "completion_tokens", 0))),
                total_tokens=safe_token_count(getattr(usage_meta, "total_tokens", 0)),
            )
        if usage.total_tokens > 0:
            return usage
    return None


def find_usage(data: Any) -> Optional[Any]:
    """递归搜索 chunk/结果结构里的用量（messages/agent/tools 键优先，倒序）。"""
    if isinstance(data, dict):
        for key in ("messages", "agent", "tools"):
            msgs = data.get(key)
            if isinstance(msgs, dict) and "messages" in msgs:
                msgs = msgs["messages"]
            if isinstance(msgs, list):
                for msg in reversed(msgs):
                    result = extract_from_message(msg)
                    if result:
                        return result
        for value in data.values():
            result = find_usage(value)
            if result:
                return result
    elif isinstance(data, (list, tuple)):
        for item in reversed(data):
            result = find_usage(item)
            if result:
                return result
    else:
        result = extract_from_message(data)
        if result:
            return result
    return None


class AgentUsageRecorder:
    """用量记录器：绑定模型，有口则记，无口静默。"""

    def __init__(self, model: Any) -> None:
        """绑定目标模型，参数 model 提供用量记录口。"""
        self.model = model

    def record_invoke_result(self, result: Dict[str, Any]) -> None:
        """从 invoke 结果中提取用量：标准元数据 → additional_kwargs 回退 → 字符估算。"""
        if not hasattr(self.model, "_record_usage"):
            return
        from .models.base import TokenUsage

        messages = result.get("messages", [])
        for msg in reversed(messages):
            usage_data = None
            if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                usage_data = msg.usage_metadata
            elif hasattr(msg, "response_metadata") and msg.response_metadata:
                meta = msg.response_metadata
                usage_data = meta.get("token_usage") or meta.get("usage")
            if usage_data:
                if isinstance(usage_data, dict):
                    usage = TokenUsage(
                        prompt_tokens=safe_token_count(usage_data.get("input_tokens", usage_data.get("prompt_tokens", 0))),
                        completion_tokens=safe_token_count(usage_data.get("output_tokens", usage_data.get("completion_tokens", 0))),
                        total_tokens=safe_token_count(usage_data.get("total_tokens", 0)),
                    )
                else:
                    usage = TokenUsage(
                        prompt_tokens=safe_token_count(getattr(usage_data, "input_tokens", getattr(usage_data, "prompt_tokens", 0))),
                        completion_tokens=safe_token_count(getattr(usage_data, "output_tokens", getattr(usage_data, "completion_tokens", 0))),
                        total_tokens=safe_token_count(getattr(usage_data, "total_tokens", 0)),
                    )
                if usage.total_tokens > 0:
                    self.model._record_usage(usage)
                    return
            if isinstance(msg, AIMessage):
                additional_kwargs = getattr(msg, "additional_kwargs", {}) or {}
                if isinstance(additional_kwargs, dict):
                    additional_usage = additional_kwargs.get("usage")
                    if additional_usage and isinstance(additional_usage, dict):
                        token_usage = TokenUsage(
                            prompt_tokens=safe_token_count(additional_usage.get("input_tokens", additional_usage.get("prompt_tokens", 0))),
                            completion_tokens=safe_token_count(additional_usage.get("output_tokens", additional_usage.get("completion_tokens", 0))),
                            total_tokens=safe_token_count(additional_usage.get("total_tokens", 0)),
                        )
                        if token_usage.total_tokens > 0:
                            self.model._record_usage(token_usage)
                            return
        self.estimate_result(result)

    def record_stream_chunk(self, chunk: Any) -> None:
        """从流式最后 chunk 中提取用量。"""
        if not hasattr(self.model, "_record_usage"):
            return
        usage = find_usage(chunk)
        if usage:
            self.model._record_usage(usage)

    def estimate_result(self, result: Dict[str, Any]) -> None:
        """API 未返用量时的字符数粗略估算。"""
        if not hasattr(self.model, "_record_usage"):
            return
        from .models.base import TokenUsage

        messages = result.get("messages", [])
        prompt_chars = 0
        completion_chars = 0
        for msg in messages:
            content = content_to_text(msg.content) if hasattr(msg, "content") else ""
            if isinstance(msg, (HumanMessage, SystemMessage)) or getattr(msg, "type", "") in ("human", "system"):
                prompt_chars += len(content)
            elif isinstance(msg, AIMessage) or getattr(msg, "type", "") in ("ai", "assistant"):
                completion_chars += len(content)
        prompt_tokens = max(1, prompt_chars // 3)
        completion_tokens = max(1, completion_chars // 3)
        self.model._record_usage(TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ))


__all__ = ["AgentUsageRecorder", "extract_from_message", "find_usage", "safe_token_count"]
