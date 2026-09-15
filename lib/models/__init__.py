"""
模型模块

提供统一的模型接入：一个声明式 provider 目录（``provider_catalog``）、
一组直接继承 LangChain 官方集成的薄协议类（``providers``），
以及由目录驱动的注册表（``registry``）。

新增 provider 通常在 ``provider_catalog.PROVIDER_CATALOG`` 里加一条即可；
新增 wire 协议则在 ``providers.PROTOCOL_SPECS`` 里加一行。
"""

from .vocabulary import ModelInfo, TokenUsage, parse_context_window
from .base import BaseModel
from .extras import ModelExtras
from .providers import (
    AnthropicModel,
    AzureOpenAIModel,
    DeepSeekModel,
    GeminiModel,
    OllamaModel,
    OpenAIModel,
    is_anthropic_available,
    is_ollama_available,
)
from .registry import (
    ModelProviderRegistry,
    ModelProviderSpec,
    get_model_provider_registry,
)

__all__ = [
    # 共享词汇表
    "BaseModel",
    "ModelExtras",
    "ModelInfo",
    "TokenUsage",
    "parse_context_window",
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
