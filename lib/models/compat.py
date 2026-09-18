"""端点兼容层 —— 非标准字段透传与兼容开关。

存在理由：**「OpenAI 兼容」不等于「完全兼容」**。各家在标准字段之外注入自己的
字段（DeepSeek 的 ``reasoning_content``、Perplexity 的 ``citations``、vLLM 的
``thinking``），而 ``langchain-openai`` 明确不保留它们 —— 多轮工具调用时推理内容
会静默丢失。

本模块提供两件事：

1. :class:`NonstandardPassthroughMixin` —— 在响应与请求之间**双向**搬运非标准字段。
   覆盖 ``_create_chat_result``（提取）与 ``_get_request_payload``（回填）两个钩子。
   搬哪些字段由 :func:`passthrough_fields` 从 ``CompatSwitches`` 算出来；总闸
   ``passthrough_nonstandard`` 关闭时两个钩子都不动作。
2. :func:`apply_compat_to_payload` —— 按 :class:`CompatSwitches` 声明调整请求
   （system role、输出上限字段、是否接受输出上限），使网关差异**用数据表达**
   而不必为每个厂商写适配代码。

两者都由 provider 目录里的 ``compat`` 字段驱动。``CompatSwitches`` 里的每个开关
都有对应的读取点与测试；**不存在「声明了但没人读」的开关**。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .provider_catalog import CompatSwitches


logger = logging.getLogger(__name__)


# 标准 OpenAI 字段 —— 上游集成已正确处理，无需我们干预
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

# 内置已知的厂商特有字段。
#
# 这不是「标准」，而是「事实标准」：OpenAI 兼容生态里最通行的那批字段名。它**不是**
# 白名单的终点 —— ``CompatSwitches.extra_passthrough_fields`` 可以在此之外补充，
# 因此新增一个端点不需要改本模块的代码。
_KNOWN_NONSTANDARD_ATTRS = frozenset({
    "reasoning_content",   # 覆盖 DeepSeek 等兼容端点的推理字段。
    "reasoning",           # 裸字段名：实测 Command Code 网关用的就是它
    "reasoning_details",   # DeepSeek / Groq 变体（结构化的 list 形式）
    "thinking",            # 部分 vLLM / 本地变体
    "citations",           # 覆盖 Perplexity 引文透传场景。
})

# 可以当「思考内容」展示的字段名，按优先级排列。
#
# 只用于**展示**，不参与透传：透传按原字段名往返，避免把 ``reasoning`` 改写成
# ``reasoning_content`` 之后被网关拒收。
REASONING_TEXT_KEYS = ("reasoning_content", "reasoning", "thinking")


def extract_reasoning_text(additional_kwargs: Any) -> str:
    """从消息的 ``additional_kwargs`` 里取出可展示的推论文本。

    三种承载形式都覆盖：纯字符串（``reasoning`` / ``reasoning_content``）、
    结构化列表（``reasoning_details`` 里若干 ``{"type": "reasoning.text", "text": ...}``）。
    取不到就返回空串 —— 展示层不需要关心厂商差异。
    """
    if not isinstance(additional_kwargs, dict):
        return ""

    for key in REASONING_TEXT_KEYS:
        value = additional_kwargs.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            joined = _join_reasoning_blocks(value)
            if joined:
                return joined

    details = additional_kwargs.get("reasoning_details")
    if isinstance(details, list):
        return _join_reasoning_blocks(details)
    return ""


def _join_reasoning_blocks(blocks: Any) -> str:
    """把结构化的推理块拼成纯文本。"""
    if not isinstance(blocks, list):
        return ""
    parts: List[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text") or block.get("content") or ""
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def _first_delta(chunk: Any) -> Dict[str, Any]:
    """从 SSE chunk 里取出第一个 choice 的 ``delta``。"""
    if not isinstance(chunk, dict):
        return {}
    choices = chunk.get("choices") or (chunk.get("chunk") or {}).get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    delta = choices[0].get("delta")
    return delta if isinstance(delta, dict) else {}


def _accumulate(existing: Any, new: Any) -> Any:
    """把同一字段的多个流式增量拼起来：字符串相接、列表追加。"""
    if existing is None:
        return new
    if isinstance(existing, str) and isinstance(new, str):
        return existing + new
    if isinstance(existing, list) and isinstance(new, list):
        return [*existing, *new]
    return new


def passthrough_fields(compat: Any) -> frozenset:
    """由兼容开关算出本次要搬运的字段名集合。

    **这是 ``passthrough_nonstandard`` 的作用点。** 总闸关闭（或对象上没有 ``compat``
    字段）时返回空集合，mixin 于是完全不动作 —— 即标准 OpenAI 行为。

    曾经的实现把总闸当装饰：mixin 无条件提取/回填，开关读了等于没读。现在字段集合
    本身由声明决定，声明之外的一律不搬（``model_extra`` 里那些计划外的键也不再被
    顺手带走），行为因此可预测、可测试。
    """
    if not isinstance(compat, CompatSwitches) or not compat.passthrough_nonstandard:
        return frozenset()
    return _KNOWN_NONSTANDARD_ATTRS | frozenset(compat.extra_passthrough_fields)


def _extract_nonstandard_fields(raw_message: Any, fields: frozenset) -> Dict[str, Any]:
    """从 OpenAI SDK 的原始响应消息对象中提取**声明过的**非标准字段。"""
    extras: Dict[str, Any] = {}
    for attr in sorted(fields):
        # hasattr 只吞 AttributeError：property 抛其他异常时会穿透，整体兜底。
        try:
            value = getattr(raw_message, attr, None)
        except Exception:
            logger.debug("读取非标准字段 %s 失败，已跳过", attr, exc_info=True)
            continue
        if value is not None and value != "":
            extras[attr] = value

    # 也捕获字典形式的非标准字段（同样只取声明过的）
    try:
        extra_data = getattr(raw_message, "model_extra", None) or {}
        for key, value in extra_data.items():
            if (
                key in fields
                and key not in _STANDARD_OPENAI_FIELDS
                and value is not None
                and value != ""
            ):
                extras[key] = value
    except Exception:
        logger.debug("读取 model_extra 失败，忽略字典形式的非标准字段", exc_info=True)
    return extras


def _strip_standard_fields(additional_kwargs: Dict[str, Any], fields: frozenset) -> Dict[str, Any]:
    """从 additional_kwargs 中挑出需要回填的字段（只保留声明过的非标准字段）。"""
    return {
        key: value
        for key, value in additional_kwargs.items()
        if key in fields and key not in _STANDARD_OPENAI_FIELDS and value is not None
    }


def _inject_nonstandard_fields(
    input_: LanguageModelInput,
    payload: Dict[str, Any],
    fields: frozenset,
) -> None:
    """把 input 消息里 additional_kwargs 的非标准字段回填到请求 payload。

    按 assistant 消息的出现顺序与 payload 中的 assistant 条目一一对应。
    """
    messages = payload.get("messages", [])
    if not isinstance(input_, (list, tuple)):
        return

    ai_messages = [message for message in input_ if isinstance(message, AIMessage)]
    ai_index = 0
    for payload_msg in messages:
        if not isinstance(payload_msg, dict):
            continue
        if payload_msg.get("role") != "assistant":
            continue
        if ai_index >= len(ai_messages):
            continue

        extras = _strip_standard_fields(
            dict(getattr(ai_messages[ai_index], "additional_kwargs", {}) or {}),
            fields,
        )
        ai_index += 1
        for key, value in extras.items():
            if key not in payload_msg and value is not None:
                payload_msg[key] = value


def apply_compat_to_payload(payload: Dict[str, Any], compat: CompatSwitches) -> None:
    """按兼容开关就地调整请求 payload。

    只做目录里显式声明过的事；默认开关（标准行为）下本函数不改变任何东西。
    """
    # 输出上限字段：端点可能改用别的字段名，或完全不接受该字段。
    if not compat.supports_max_output_tokens:
        payload.pop("max_tokens", None)
        payload.pop("max_completion_tokens", None)
    elif compat.max_tokens_field != "max_tokens" and "max_tokens" in payload:
        payload[compat.max_tokens_field] = payload.pop("max_tokens")

    # system prompt 的承载 role。
    if compat.system_role != "system":
        for payload_msg in payload.get("messages", []):
            if isinstance(payload_msg, dict) and payload_msg.get("role") == "system":
                payload_msg["role"] = compat.system_role


class NonstandardPassthroughMixin:
    """为非标准字段提供双向透传的 mixin。

    必须放在 LangChain 具体类**之前**，使覆盖生效并让 ``super()`` 落到上游实现：

        class OpenAIModel(NonstandardPassthroughMixin, ModelExtras, ChatOpenAI): ...

    非 OpenAI 协议（Anthropic / Gemini / Ollama）不需要本 mixin。

    **本 mixin 由实例上的 ``compat`` 字段驱动。** 没有该字段、或
    ``passthrough_nonstandard`` 关闭时，三个钩子都是空操作。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: Dict[str, Any],
        default_chunk_class: Any,
        base_generation_info: Optional[Dict[str, Any]],
    ) -> Any:
        """流式路径上的非标准字段提取。

        **为什么必须有这个钩子。** ``_create_chat_result`` 只在非流式时被调用；
        流式走的是 ``_convert_chunk_to_generation_chunk``，而 ``langchain-openai``
        在这里只认标准字段 —— 厂商特有的推理字段会被**整块丢掉**。实测该网关
        一次回答里 47 个 chunk 带 ``reasoning``、只有 7 个带 ``content``：
        也就是说**大部分流式内容在集成层就被扔了**，调用方只看到一串空 chunk。

        这里按**原字段名**把增量累积回 ``additional_kwargs``（字符串拼接、列表追加），
        因此回填时仍是厂商原本的字段名，不会被改写成别的名字而遭网关拒收。
        """
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )

        try:
            fields = passthrough_fields(getattr(self, "compat", None))
            if not fields or generation is None:
                return generation

            delta = _first_delta(chunk)
            if not delta:
                return generation

            extras = {
                key: value
                for key, value in delta.items()
                if key in fields and value not in (None, "", [])
            }
            if not extras:
                return generation

            message = getattr(generation, "message", None)
            if message is None:
                return generation

            merged = dict(getattr(message, "additional_kwargs", None) or {})
            for key, value in extras.items():
                merged[key] = _accumulate(merged.get(key), value)
            object.__setattr__(message, "additional_kwargs", merged)
        except Exception:
            logger.debug("流式提取非标准字段失败", exc_info=True)

        return generation

    def _create_chat_result(
        self,
        response: Any,
        generation_info: Optional[Dict[str, Any]] = None,
    ) -> ChatResult:
        """在父类生成 ChatResult 之后，把非标准字段并入 AIMessage.additional_kwargs。"""
        result = super()._create_chat_result(response, generation_info)

        try:
            fields = passthrough_fields(getattr(self, "compat", None))
            if fields:
                choices = getattr(response, "choices", None)
                if choices:
                    raw_message = getattr(choices[0], "message", None)
                    if raw_message is not None:
                        extras = _extract_nonstandard_fields(raw_message, fields)
                        if extras:
                            for generation in result.generations:
                                if not isinstance(generation, ChatGeneration):
                                    continue
                                message = generation.message
                                if isinstance(message, AIMessage):
                                    merged = dict(message.additional_kwargs or {})
                                    merged.update(extras)
                                    object.__setattr__(message, "additional_kwargs", merged)
        except Exception:
            logger.debug("提取响应中的非标准字段失败", exc_info=True)

        return result

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """在父类生成 payload 之后回填非标准字段，并应用兼容开关。"""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        compat = getattr(self, "compat", None)

        try:
            fields = passthrough_fields(compat)
            if fields:
                _inject_nonstandard_fields(input_, payload, fields)
        except Exception:
            logger.debug("回填非标准字段失败", exc_info=True)

        if isinstance(compat, CompatSwitches):
            try:
                apply_compat_to_payload(payload, compat)
            except Exception:
                logger.debug("应用兼容开关失败", exc_info=True)

        return payload

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """声明该 mixin 不参与 LangChain 序列化。"""
        return False


__all__ = [
    "NonstandardPassthroughMixin",
    "REASONING_TEXT_KEYS",
    "apply_compat_to_payload",
    "extract_reasoning_text",
    "passthrough_fields",
    "_extract_nonstandard_fields",
    "_inject_nonstandard_fields",
    "_strip_standard_fields",
]
