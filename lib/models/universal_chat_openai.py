"""
UniversalChatOpenAI — 通用 additional_kwargs 透传包装器。

覆盖 langchain-openai 的 ChatOpenAI 的两个关键方法：
1. `_create_chat_result` → 从 API 原始响应中提取非标准字段到 additional_kwargs
2. `_get_request_payload` → 将 additional_kwargs 中的非标准字段写回 API 请求

标准 OpenAI 字段（tool_calls/function_call/refusal/parsed/usage/token_usage）
不会被重复注入——它们已由 ChatOpenAI 正确处理。

非标准字段（reasoning_content、reasoning_details、citations 等第三方提供商返回的任意字段）
会自动被提取和回传，无需 per-provider 特殊处理。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ==============================================================================
# 标准 OpenAI 字段 — ChatOpenAI 已经正确处理，不需要我们干预
# ==============================================================================

_STANDARD_OPENAI_FIELDS = frozenset({
    "tool_calls",
    "function_call",
    "refusal",
    "parsed",
    "name",
    "usage",
    "token_usage",
    "finish_reason",
    "audio",
})

# OpenAI SDK 响应对象的已知非标准属性（各厂商注入的额外字段）
_NONSTANDARD_RESPONSE_ATTRS = frozenset({
    "reasoning_content",   # DeepSeek
    "reasoning_details",   # DeepSeek / Groq variants
    "thinking",            # some vLLM / local variants
    "citations",           # Perplexity
})


def _extract_nonstandard_fields(raw_message: Any) -> dict[str, Any]:
    """从 OpenAI SDK 的原始响应消息对象中提取非标准字段。"""
    extras: dict[str, Any] = {}
    for attr in _NONSTANDARD_RESPONSE_ATTRS:
        if hasattr(raw_message, attr):
            val = getattr(raw_message, attr)
            if val is not None and val != "":
                extras[attr] = val
    # 也会捕获字典形式的非标准字段
    if hasattr(raw_message, "model_extra"):
        try:
            extra_data = raw_message.model_extra or {}
            for key, val in extra_data.items():
                if key not in _STANDARD_OPENAI_FIELDS and val is not None and val != "":
                    extras[key] = val
        except Exception:
            pass
    return extras


def _strip_standard_fields(additional_kwargs: dict[str, Any]) -> dict[str, Any]:
    """从 additional_kwargs 中移除标准 OpenAI 字段，只保留非标准透传字段。"""
    return {
        k: v for k, v in additional_kwargs.items()
        if k not in _STANDARD_OPENAI_FIELDS and v is not None
    }


class UniversalChatOpenAI(ChatOpenAI):
    """ChatOpenAI 的子类，自动透传所有非标准 additional_kwargs 字段。

    解决 langchain-openai 的 ChatOpenAI 明确不提取/不保留 reasoning_content
    等第三方厂商特有字段的问题。

    用法：
        model = UniversalChatOpenAI(
            model="deepseek-chat",
            openai_api_base="https://api.deepseek.com/v1",
            openai_api_key="...",
        )
        # 或用于任何 OpenAI 兼容接口：
        model = UniversalChatOpenAI(
            model="local-model",
            openai_api_base="http://localhost:8000/v1",
            openai_api_key="...",
        )
    """

    def _create_chat_result(
        self,
        response: Any,
        generation_info: Optional[dict[str, Any]] = None,
    ) -> ChatResult:
        """在父类生成 ChatResult 之后，从原始响应中提取非标准字段。

        注入到 AIMessage.additional_kwargs 中，确保 reasoning_content
        等字段不会在下一次 API 请求中被丢弃。
        """
        result = super()._create_chat_result(response, generation_info)

        # 从原始 OpenAI SDK 响应中提取非标准字段
        try:
            if hasattr(response, "choices") and response.choices:
                choice = response.choices[0]
                if hasattr(choice, "message"):
                    raw_message = choice.message
                    extras = _extract_nonstandard_fields(raw_message)

                    if extras:
                        for generation in result.generations:
                            if isinstance(generation, ChatGeneration):
                                msg = generation.message
                                if isinstance(msg, AIMessage):
                                    existing = dict(msg.additional_kwargs or {})
                                    existing.update(extras)
                                    object.__setattr__(
                                        msg, "additional_kwargs", existing,
                                    )
        except Exception:
            logger.debug("Failed to extract non-standard fields from response",
                         exc_info=True)

        return result

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> dict:
        """在父类生成 payload 之后，将 additional_kwargs 中的非标准字段注入消息。"""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            _inject_nonstandard_fields(input_, payload)
        except Exception:
            logger.debug("Failed to inject non-standard fields into payload",
                         exc_info=True)
        return payload

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return False


# ==============================================================================
# ChatDeepSeek 透传修复
# ==============================================================================
# ChatDeepSeek 正确提取 reasoning_content 到 additional_kwargs，
# 但 _get_request_payload 没有把它写回 API 请求。
# 这个子类修复注入端。

try:
    from langchain_deepseek import ChatDeepSeek

    class UniversalChatDeepSeek(ChatDeepSeek):
        """ChatDeepSeek 子类，在 API 请求中注入 reasoning_content 等非标准字段。

        ChatDeepSeek 已正确从 API 响应中提取 reasoning_content 存入
        additional_kwargs。但它覆盖的 _get_request_payload 只处理了
        message content 格式（list→string），没有把 additional_kwargs
        写回消息 dict。这个类补全该行为。
        """

        def _get_request_payload(
            self,
            input_: LanguageModelInput,
            *,
            stop: Optional[List[str]] = None,
            **kwargs: Any,
        ) -> dict:
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            try:
                _inject_nonstandard_fields(input_, payload)
            except Exception:
                logger.debug(
                    "Failed to inject non-standard fields into DeepSeek payload",
                    exc_info=True,
                )
            return payload

        @classmethod
        def is_lc_serializable(cls) -> bool:
            return False

except ImportError:
    UniversalChatDeepSeek = None  # type: ignore[assignment,misc]


def _inject_nonstandard_fields(
    input_: LanguageModelInput, payload: dict
) -> None:
    """将 input 消息中 additional_kwargs 的非标准字段注入 payload 消息。"""
    messages = payload.get("messages", [])
    if not isinstance(input_, (list, tuple)):
        return
    ai_messages = [m for m in input_ if isinstance(m, AIMessage)]
    ai_index = 0
    for payload_msg in messages:
        if not isinstance(payload_msg, dict):
            continue
        if payload_msg.get("role") != "assistant":
            continue
        if ai_index >= len(ai_messages):
            continue
        input_msg = ai_messages[ai_index]
        ai_index += 1
        extras = _strip_standard_fields(
            dict(getattr(input_msg, "additional_kwargs", {}) or {}),
        )
        for key, val in extras.items():
            if key not in payload_msg and val is not None:
                payload_msg[key] = val


__all__ = [
    "UniversalChatDeepSeek",
    "UniversalChatOpenAI",
    "_extract_nonstandard_fields",
    "_inject_nonstandard_fields",
    "_strip_standard_fields",
]
