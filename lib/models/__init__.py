"""
模型模块

提供统一的模型接口，支持多种模型类型（Ollama、OpenAI、Anthropic 等）。
模型创建统一通过 ModelProviderRegistry。
"""

from .base import BaseModel, ModelInfo, parse_context_window
from .ollama_model import OllamaModel
from .openai_model import OpenAIModel, AzureOpenAIModel
from .gemini_model import GeminiModel
from .registry import (
    ModelProviderRegistry,
    ModelProviderSpec,
    get_model_provider_registry,
    is_anthropic_available,
)

try:
    from .anthropic_model import AnthropicModel
except ImportError:
    AnthropicModel = None

__all__ = [
    "BaseModel",
    "ModelInfo",
    "parse_context_window",
    "OllamaModel",
    "OpenAIModel",
    "AzureOpenAIModel",
    "GeminiModel",
    "AnthropicModel",
    "ModelProviderRegistry",
    "ModelProviderSpec",
    "get_model_provider_registry",
    "is_anthropic_available",
]
