"""端点兼容层，非标准字段透传与兼容开关。

存在理由，各家开放兼容端点并不完全兼容，会在标准字段外注入自有字段，
而上游集成不保留它们，多轮工具调用时推理内容会静默丢失。

本模块提供两件事，非标准字段在响应与请求之间双向搬运，
覆盖提取与回填两个钩子，搬哪些字段由兼容开关算出，总闸关闭时都不动作。
按兼容开关声明调整请求，使网关差异用数据表达，不必为每个厂商写适配代码。

两者都由目录里的兼容字段驱动，每个开关都有读取点与测试，不存在声明了没人读的开关。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .provider_catalog import CompatSwitches


logger = logging.getLogger(__name__)


# 标准开放字段，上游已正确处理，无需干预
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
# 这是事实标准，即生态里最通行的那批字段名，不是终点。
# 兼容开关的额外字段可在之外补充，新增端点不需改本模块。
_KNOWN_NONSTANDARD_ATTRS = frozenset({
    "reasoning_content",   # 覆盖 DeepSeek 等兼容端点的推理字段。
    "reasoning",           # 裸字段名，部分网关用的就是它
    "reasoning_details",   # DeepSeek / Groq 变体（结构化的 list 形式）
    "thinking",            # 部分 vLLM / 本地变体
    "citations",           # 覆盖 Perplexity 引文透传场景。
})

# 可以当思考内容展示的字段名，按优先级排列。
#
# 只用于展示，不参与透传，透传按原字段名往返，避免改写后被网关拒收。
REASONING_TEXT_KEYS = ("reasoning_content", "reasoning", "thinking")


def extract_reasoning_text(additional_kwargs: object) -> str:
    # 参数用 object，输入是各家消息上挂的未知附加字段，函数内已做类型分支
    """从消息附加字段里取出可展示的推论文本。

    三种承载形式都覆盖，纯字符串，结构化列表。
    取不到返回空串，展示层不需关心厂商差异。
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


def _join_reasoning_blocks(blocks: object) -> str:
    # 参数用 object，结构化推理块可能是字符串列表或字典列表，函数内已分支
    """把结构化的推理块拼成纯文本。"""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text") or block.get("content") or ""
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def _first_delta(chunk: object) -> dict[str, Any]:
    # 参数用 object，输入是上游 SSE 事件的未知形态，函数内已做类型分支
    """从事件块里取出第一个选项的增量。"""
    if not isinstance(chunk, dict):
        return {}
    choices = chunk.get("choices") or (chunk.get("chunk") or {}).get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    delta = choices[0].get("delta")
    return delta if isinstance(delta, dict) else {}


def _accumulate(existing: object, new: object) -> object:
    # 参数用 object，同一字段的增量可能是字符串或列表，函数内已按类型拼接
    """把同一字段的多个流式增量拼起来：字符串相接、列表追加。"""
    if existing is None:
        return new
    if isinstance(existing, str) and isinstance(new, str):
        return existing + new
    if isinstance(existing, list) and isinstance(new, list):
        return [*existing, *new]
    return new


def passthrough_fields(compat: object) -> frozenset[str]:
    # 参数用 object，实例上可能没有 compat 字段，函数内已用 isinstance 收窄
    """由兼容开关算出本次要搬运的字段名集合。

    总闸关闭或对象上没有兼容字段时返回空集合，混入层完全不动作。
    字段集合本身由声明决定，声明之外一律不搬，行为可预测可测试。
    """
    if not isinstance(compat, CompatSwitches) or not compat.passthrough_nonstandard:
        return frozenset()
    return _KNOWN_NONSTANDARD_ATTRS | frozenset(compat.extra_passthrough_fields)


def _extract_nonstandard_fields(raw_message: object, fields: frozenset[str]) -> dict[str, Any]:
    # 第一个参数用 object，输入是厂商 SDK 的原始响应对象，形态不统一，只能用 getattr 探测
    """从原始响应消息对象中提取声明过的非标准字段。"""
    extras: dict[str, Any] = {}
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


def _strip_standard_fields(additional_kwargs: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    """从 additional_kwargs 中挑出需要回填的字段（只保留声明过的非标准字段）。"""
    return {
        key: value
        for key, value in additional_kwargs.items()
        if key in fields and key not in _STANDARD_OPENAI_FIELDS and value is not None
    }


def _inject_nonstandard_fields(
    input_: LanguageModelInput,
    payload: dict[str, Any],
    fields: frozenset[str],
) -> None:
    """把输入消息里附加字段的非标准字段回填到请求载荷。

    按助手消息的出现顺序与载荷中的助手条目一一对应。
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


def apply_compat_to_payload(payload: dict[str, Any], compat: CompatSwitches) -> None:
    """按兼容开关就地调整请求载荷。

    只做目录里显式声明过的事，默认开关下不改变任何东西。
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
    """为非标准字段提供双向透传的混入。

    必须放在具体类之前，使覆盖生效并让调用落到上游实现。
    非开放协议不需要本混入。
    本混入由实例上的兼容字段驱动，没有该字段或总闸关闭时，三个钩子都是空操作。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        # 组块类透传给上游实现，形态由各家集成决定，保持 Any
        default_chunk_class: Any,
        base_generation_info: Optional[dict[str, Any]],
    ) -> Any:
        """流式路径上的非标准字段提取。

        非流式结果钩子只在非流式时调用，流式走另一个转换钩子，
        而上游在那里只认标准字段，厂商特有推理字段会被整块丢掉。
        部分回答里多数块带推理字段、少数带正文，不处理则调用方只看到一串空块。
        这里按原字段名把增量累积回去，字符串拼接，列表追加，
        回填时仍是厂商原本的字段名，不会因改写而遭拒收。
        """
        # 被覆盖的方法由 MRO 后面的厂商集成类提供，单独检查混入类时静态找不到
        generation = super()._convert_chunk_to_generation_chunk(  # type: ignore[misc]
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
        # 响应是厂商 SDK 的原始对象，形态不统一，只能用 getattr 探测，保持 Any
        response: Any,
        generation_info: Optional[dict[str, Any]] = None,
    ) -> ChatResult:
        """在父类生成结果之后，把非标准字段并入消息附加字段。"""
        # 被覆盖的方法由 MRO 后面的厂商集成类提供，单独检查混入类时静态找不到
        result = super()._create_chat_result(response, generation_info)  # type: ignore[misc]

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
        stop: Optional[list[str]] = None,
        # 请求参数透传给上游实现，键形态由各家集成决定，保持 Any
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在父类生成载荷之后回填非标准字段，并应用兼容开关。"""
        # 被覆盖的方法由 MRO 后面的厂商集成类提供，单独检查混入类时静态找不到
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)  # type: ignore[misc]

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
