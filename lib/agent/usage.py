"""用量记录归属 lib.agent 包，由 usage 模块承载。

归属：models 侧 helper（只搬运，不重写语义）。
原实现逐行搬自 lib.agent_recovery（safe_token_count / _find_usage /
record_invoke_result / record_stream_chunk / estimate_result），
直调 models.vocabulary 是用量提取的唯一入口。

调用链：
agent_loop.invoke_with_messages（非流执行尾部）
→ record_invoke_result（标准 usage_metadata 优先，取不到则字符估算）；
agent_loop.stream_turn（流执行尾部，拿到 last_chunk 后）
→ record_stream_chunk（递归搜索 chunk 结构里的用量）；
模型无 _record_usage 时静默跳过（兼容无用量能力的模型替身）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..core.agent_runtime import content_to_text


def safe_token_count(value: Any) -> int:
    """规范化可选或 provider 特有的 token 计数器，且不让 turn 失败。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _find_usage(data: Any) -> Optional[Any]:
    """递归搜索 chunk/结果结构里的用量（messages/agent/tools 键优先，倒序）。"""
    from ..models.vocabulary import token_usage_from_message

    if isinstance(data, dict):
        for key in ("messages", "agent", "tools"):
            msgs = data.get(key)
            if isinstance(msgs, dict) and "messages" in msgs:
                msgs = msgs["messages"]
            if isinstance(msgs, list):
                for msg in reversed(msgs):
                    result = token_usage_from_message(msg)
                    if result:
                        return result
        for value in data.values():
            result = _find_usage(value)
            if result:
                return result
    elif isinstance(data, (list, tuple)):
        for item in reversed(data):
            result = _find_usage(item)
            if result:
                return result
    else:
        result = token_usage_from_message(data)
        if result:
            return result
    return None


def record_invoke_result(model: Any, result: Dict[str, Any]) -> None:
    """从 invoke 结果中提取用量：标准元数据优先，取不到则字符估算。"""
    if not hasattr(model, "_record_usage"):
        return
    from ..models.vocabulary import token_usage_from_message

    messages = result.get("messages", [])
    for msg in reversed(messages):
        usage = token_usage_from_message(msg)
        if usage and usage.total_tokens > 0:
            model._record_usage(usage)
            return
    estimate_result(model, result)


def record_stream_chunk(model: Any, chunk: Any) -> None:
    """从流式最后 chunk 中提取用量。"""
    if not hasattr(model, "_record_usage"):
        return
    usage = _find_usage(chunk)
    if usage:
        model._record_usage(usage)


def estimate_result(model: Any, result: Dict[str, Any]) -> None:
    """API 未返用量时的字符数粗略估算。"""
    if not hasattr(model, "_record_usage"):
        return
    from ..models.base import TokenUsage

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
    model._record_usage(TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    ))


__all__ = [
    "estimate_result",
    "record_invoke_result",
    "record_stream_chunk",
    "safe_token_count",
]
