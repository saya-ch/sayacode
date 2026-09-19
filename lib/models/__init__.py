"""
模型模块

提供统一的模型接入，一个声明式接入目录，一组直接继承官方集成的薄协议类，
以及由目录驱动的注册表。
新增接入方通常在目录里加一条即可，新增传输协议在协议差异表里加一行。
协议类惰性导出，首次访问时才导入对应依赖，导入本模块不拖任何厂商包。
"""

from .vocabulary import (
    ModelInfo,
    TokenUsage,
    parse_context_window,
    token_usage_from_mapping,
    token_usage_from_message,
)
from typing import Any
from .base import BaseModel
from .extras import ModelExtras
from .providers import (
    _PROTOCOL_CLASS_NAMES,
    is_anthropic_available,
    is_ollama_available,
)
from .registry import (
    ModelProviderRegistry,
    ModelProviderSpec,
    get_model_provider_registry,
)

_LAZY_PROTOCOL_CLASSES = frozenset(_PROTOCOL_CLASS_NAMES)


def __getattr__(name: str) -> Any:
    # 返回动态解析的协议类，各家类型不统一，保持 Any
    """模块懒解析，协议类首次访问时才解析。"""
    if name in _LAZY_PROTOCOL_CLASSES:
        from . import providers

        return getattr(providers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # 共享词汇表
    "BaseModel",
    "ModelExtras",
    "ModelInfo",
    "TokenUsage",
    "parse_context_window",
    "token_usage_from_mapping",
    "token_usage_from_message",
    # 协议实现
    "AnthropicModel",
    "AzureOpenAIModel",
    "DeepSeekModel",
    "GeminiModel",
    "OllamaModel",
    "OpenAIModel",
    # 注册表
    "ModelProviderRegistry",
    "ModelProviderSpec",
    "get_model_provider_registry",
    # 可选依赖探测
    "is_anthropic_available",
    "is_ollama_available",
]
