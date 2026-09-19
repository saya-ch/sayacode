"""模型层的共享词汇表 —— Token 用量、模型信息、上下文窗口解析。

独立成模块是为了打断循环导入：extras 需要这些类型，而 base 需要
extras 的共享实现。本模块不依赖包内其他模块。

lib.models.base 会重新导出这里的名字，因此
from lib.models.base import TokenUsage, ModelInfo, parse_context_window
仍然是有效的导入路径。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional


_CONTEXT_WINDOW_SUFFIXES = {
    "": 1,
    "k": 1024,
    "m": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
}
_MAX_CONTEXT_WINDOW = 100_000_000


def parse_context_window(value: object) -> Optional[int]:
    """
    解析模型上下文窗口值。

    参数必须是 object（而非更具体的联合类型）：输入来自用户配置与各家
    API 的原始值，可能是 int / float / str / None，乃至布尔与嵌套结构，
    函数内部已用 isinstance 全部分支，非法输入一律返回 None。
    解析模型上下文窗口值。

    可接受的示例：
    - 128000
    - "128000"
    - "128,000"
    - "256k" / "256K"
    - "1M" / "1.5m"

    后缀使用基于 1024 的上下文记法，因为大多数模型窗口都以
    32K/128K/1M 这种 2 的幂形式宣传。需要精确十进制值的用户
    可以直接输入纯数字。
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value > 0 else None

    if isinstance(value, float):
        return int(value) if value > 0 and value.is_integer() else None

    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None

    text = text.replace(",", "").replace("_", "").strip()
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*([kKmMgG]?)\s*(?:tokens?|token|t)?",
        text,
    )
    if not match:
        return None

    number_text, suffix = match.groups()
    multiplier = _CONTEXT_WINDOW_SUFFIXES.get(suffix.lower())
    if multiplier is None:
        return None

    try:
        number = float(number_text)
    except ValueError:
        return None

    if number <= 0:
        return None

    if suffix == "" and not number.is_integer():
        return None

    parsed = int(number * multiplier)
    if parsed > _MAX_CONTEXT_WINDOW:
        return None
    return parsed if parsed > 0 else None


@dataclass
class TokenUsage:
    """单次调用的 Token 用量统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class ModelInfo:
    """模型信息数据结构"""

    # 模型名称
    name: str

    # 模型类型（ollama/openai 等）
    model_type: str

    # 模型供应商
    provider: str

    # 模型支持的参数
    supported_params: list[str]

    # 是否支持流式输出
    supports_streaming: bool = True

    # 其他元数据：值是各 provider 自带异构键，无法静态定型，故保持 Any。
    metadata: Optional[dict[str, Any]] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


def _read_int_value(source: object, key: str) -> int:
    """从 dict 或对象上读一个整数字段，缺失或非法时返回 0。

    参数必须是 object：调用方传入的是各家 SDK 的用量映射，
    可能是 dict，也可能是带属性的对象，内部已用 isinstance 分支。
    """
    if isinstance(source, dict):
        raw = source.get(key, 0)
    else:
        raw = getattr(source, key, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def token_usage_from_mapping(source: object) -> Optional["TokenUsage"]:
    """从用量映射（dict 或对象）构造 TokenUsage，无有效用量时返回 None。

    参数必须是 object：唯一可信源是 LangChain 标准 usage_metadata
    形状优先（input_tokens / output_tokens / total_tokens），
    兼容 OpenAI 风格的 prompt_tokens / completion_tokens 别名；
    上游各家 SDK 给出的可能是 dict 也可能是属性对象，调用方只需传已摘出的
    映射，不用再分支 dict 与对象。
    """
    if source is None:
        return None
    if isinstance(source, dict):
        prompt = _read_int_value(source, "input_tokens") or _read_int_value(source, "prompt_tokens")
        completion = _read_int_value(source, "output_tokens") or _read_int_value(source, "completion_tokens")
        total = _read_int_value(source, "total_tokens") or (prompt + completion)
    else:
        prompt = _read_int_value(source, "input_tokens") or _read_int_value(source, "prompt_tokens")
        completion = _read_int_value(source, "output_tokens") or _read_int_value(source, "completion_tokens")
        total = _read_int_value(source, "total_tokens") or (prompt + completion)
    if total <= 0:
        return None
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def token_usage_from_message(msg: object) -> Optional["TokenUsage"]:
    """从单条 LangChain 消息提取用量，无则返回 None。

    参数必须是 object（而非 BaseMessage）：函数只做防御式 getattr 探测，
    调用方传入的可能是 AIMessage / chunk / 响应对象乃至 None，
    查找顺序与官方一致：usage_metadata → response_metadata
    （token_usage / usage）→ additional_kwargs["usage"]。
    """
    if msg is None:
        return None
    usage_meta = getattr(msg, "usage_metadata", None)
    if usage_meta:
        usage = token_usage_from_mapping(usage_meta)
        if usage:
            return usage
    response_meta = getattr(msg, "response_metadata", None)
    if isinstance(response_meta, dict):
        nested = response_meta.get("token_usage") or response_meta.get("usage")
        if nested:
            usage = token_usage_from_mapping(nested)
            if usage:
                return usage
    additional = getattr(msg, "additional_kwargs", None)
    if isinstance(additional, dict):
        nested = additional.get("usage")
        if isinstance(nested, dict):
            usage = token_usage_from_mapping(nested)
            if usage:
                return usage
    return None


__all__ = [
    "ModelInfo",
    "TokenUsage",
    "parse_context_window",
    "token_usage_from_mapping",
    "token_usage_from_message",
]
