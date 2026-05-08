"""Provider catalog shared by model startup, profiles, and config UI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ProviderCatalogEntry:
    """Static model provider metadata."""

    value: str
    label: str
    description: str
    default_base_url: str
    default_model_name: str
    api_key_env: Optional[str]
    requires_api_key: bool
    endpoint: str
    aliases: tuple[str, ...] = ()
    requires_base_url: bool = False
    requires_package: Optional[str] = None
    visible: bool = True
    base_url_env: Optional[str] = None

    def resolved_default_base_url(self) -> str:
        if self.base_url_env:
            return os.environ.get(self.base_url_env, self.default_base_url)
        return self.default_base_url

    def runtime_default_base_url(self) -> Optional[str]:
        if self.requires_base_url:
            return None
        return self.resolved_default_base_url()


PROVIDER_CATALOG: Dict[str, ProviderCatalogEntry] = {
    "openai": ProviderCatalogEntry(
        value="openai",
        label="OpenAI",
        description="Hosted and OpenAI-compatible APIs",
        default_base_url="https://api.openai.com/v1",
        default_model_name="gpt-4",
        api_key_env="OPENAI_API_KEY",
        requires_api_key=True,
        endpoint="/v1/chat/completions",
    ),
    "anthropic": ProviderCatalogEntry(
        value="anthropic",
        label="Anthropic",
        description="Claude and Anthropic-compatible endpoints",
        default_base_url="https://api.anthropic.com/v1",
        default_model_name="claude-sonnet-4-20250514",
        api_key_env="ANTHROPIC_API_KEY",
        requires_api_key=True,
        endpoint="/v1/messages",
        requires_package="langchain-anthropic",
    ),
    "azure_openai": ProviderCatalogEntry(
        value="azure_openai",
        label="Azure OpenAI",
        description="Azure-hosted OpenAI deployments",
        default_base_url="https://<your-resource>.openai.azure.com/v1",
        default_model_name="gpt-4",
        api_key_env="AZURE_OPENAI_API_KEY",
        requires_api_key=True,
        endpoint="/v1/chat/completions",
        aliases=("azure",),
        requires_base_url=True,
        visible=False,
    ),
    "gemini": ProviderCatalogEntry(
        value="gemini",
        label="Google Gemini",
        description="Gemini API with Google-hosted models",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model_name="gemini-2.5-flash",
        api_key_env="GEMINI_API_KEY",
        requires_api_key=True,
        endpoint="/models/{model}:generateContent",
    ),
    "ollama": ProviderCatalogEntry(
        value="ollama",
        label="Ollama",
        description="Local models running on your machine",
        default_base_url="http://localhost:11434",
        default_model_name="qwen3.5:9b",
        api_key_env=None,
        requires_api_key=False,
        endpoint="/api/chat",
        requires_package="langchain-ollama",
        base_url_env="OLLAMA_BASE_URL",
    ),
    "generic": ProviderCatalogEntry(
        value="generic",
        label="Generic OpenAI Compatible",
        description="Custom OpenAI-compatible endpoint",
        default_base_url="https://your-api-endpoint/v1",
        default_model_name="gpt-4",
        api_key_env="OPENAI_API_KEY",
        requires_api_key=True,
        endpoint="/v1/chat/completions",
        requires_base_url=True,
        visible=False,
    ),
}

USER_VISIBLE_PROVIDER_TYPES = tuple(
    key for key, entry in PROVIDER_CATALOG.items() if entry.visible
)


def normalize_provider_type(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    normalized = str(value or "").lower().strip()
    return "azure_openai" if normalized == "azure" else normalized


def provider_catalog_entry(value: Any) -> ProviderCatalogEntry:
    normalized = normalize_provider_type(value)
    if normalized in PROVIDER_CATALOG:
        return PROVIDER_CATALOG[normalized]
    return PROVIDER_CATALOG["ollama"]


def provider_defaults(value: Any) -> Dict[str, Any]:
    entry = provider_catalog_entry(value)
    return {
        "label": entry.label,
        "value": entry.value,
        "description": entry.description,
        "default_base_url": entry.resolved_default_base_url(),
        "runtime_default_base_url": entry.runtime_default_base_url(),
        "default_model_name": entry.default_model_name,
        "api_key_env": entry.api_key_env,
        "requires_api_key": entry.requires_api_key,
        "requires_base_url": entry.requires_base_url,
        "endpoint": entry.endpoint,
    }


def visible_provider_options() -> list[Dict[str, Any]]:
    return [provider_defaults(value) for value in USER_VISIBLE_PROVIDER_TYPES]


__all__ = [
    "PROVIDER_CATALOG",
    "USER_VISIBLE_PROVIDER_TYPES",
    "ProviderCatalogEntry",
    "normalize_provider_type",
    "provider_catalog_entry",
    "provider_defaults",
    "visible_provider_options",
]
