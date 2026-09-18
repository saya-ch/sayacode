"""
模型模块

提供统一的模型接入：一个声明式 provider 目录（``provider_catalog``）、
一组直接继承 LangChain 官方集成的薄协议类（``providers``），
以及由目录驱动的注册表（``registry``）。

新增 provider 通常在 ``provider_catalog.PROVIDER_CATALOG`` 里加一条即可；
新增 wire 协议则在 ``providers.PROTOCOL_SPECS`` 里加一行。

协议类是惰性导出的：``from lib.models import OpenAIModel`` 首次访问时才
import 对应厂商 SDK。``import lib.models`` 本身不拖任何厂商包。
"""

from .vocabulary import (
    ModelInfo,
    TokenUsage,
    parse_context_window,
    token_usage_from_mapping,
    token_usage_from_message,
)
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


def __getattr__(name: str):
    """PEP 562：协议类首次访问时才解析（拖入对应厂商 SDK）。"""
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
