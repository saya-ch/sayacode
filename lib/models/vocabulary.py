"""模型层的共享词汇表 —— Token 用量、模型信息、上下文窗口解析。

独立成模块是为了打断循环导入：``extras`` 需要这些类型，而 ``base`` 需要
``extras`` 的共享实现。本模块**不依赖包内其他模块**。

``lib.models.base`` 会重新导出这里的名字，因此
``from lib.models.base import TokenUsage, ModelInfo, parse_context_window``
仍然是有效的导入路径。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


_CONTEXT_WINDOW_SUFFIXES = {
    "": 1,
    "k": 1024,
    "m": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
}
_MAX_CONTEXT_WINDOW = 100_000_000


def parse_context_window(value: Any) -> Optional[int]:
    """
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
    supported_params: List[str]

    # 是否支持流式输出
    supports_streaming: bool = True

    # 其他元数据
    metadata: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


__all__ = ["ModelInfo", "TokenUsage", "parse_context_window"]
