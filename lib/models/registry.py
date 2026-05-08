"""Single model provider registry for SAYACODE."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Type, Union

from .base import BaseModel, parse_context_window
from .gemini_model import GeminiModel
from .ollama_model import OllamaModel
from .openai_model import AzureOpenAIModel, OpenAIModel
from .provider_catalog import provider_catalog_entry

try:
    from .anthropic_model import AnthropicModel, is_anthropic_available
except ImportError:
    AnthropicModel = None

    def is_anthropic_available() -> bool:
        return False

try:
    from .factory_models import GenericOpenAIModel
except ImportError:
    GenericOpenAIModel = None


@dataclass(frozen=True)
class ModelProviderSpec:
    """One registered model provider."""

    key: str
    model_class: Optional[Type[BaseModel]]
    display_name: str
    aliases: tuple[str, ...] = ()
    default_base_url: Optional[str] = None
    default_model_name: Optional[str] = None
    requires_api_key: bool = False
    env_var: Optional[str] = None
    requires_package: Optional[str] = None
    requires_base_url: bool = False


class ModelProviderRegistry:
    """Create and inspect model providers through one registry."""

    def __init__(self, providers: Optional[Iterable[ModelProviderSpec]] = None) -> None:
        self._providers: Dict[str, ModelProviderSpec] = {}
        self._aliases: Dict[str, str] = {}
        for provider in providers or ():
            self.register(provider)

    def register(self, provider: ModelProviderSpec) -> None:
        key = self.normalize_type(provider.key)
        normalized = ModelProviderSpec(
            key=key,
            model_class=provider.model_class,
            display_name=provider.display_name,
            aliases=tuple(self.normalize_type(alias) for alias in provider.aliases),
            default_base_url=provider.default_base_url,
            default_model_name=provider.default_model_name,
            requires_api_key=provider.requires_api_key,
            env_var=provider.env_var,
            requires_package=provider.requires_package,
            requires_base_url=provider.requires_base_url,
        )
        self._providers[key] = normalized
        self._aliases[key] = key
        for alias in normalized.aliases:
            self._aliases[alias] = key

    def normalize_type(self, api_type: Union[str, Any]) -> str:
        if hasattr(api_type, "value"):
            api_type = api_type.value
        normalized = str(api_type or "").lower().strip()
        return "azure" if normalized == "azure_openai" else normalized

    def get(self, api_type: Union[str, Any]) -> ModelProviderSpec:
        key = self._aliases.get(self.normalize_type(api_type))
        if not key or key not in self._providers:
            raise ValueError(
                f"不支持的模型类型: {api_type}。"
                f"支持的类型: {self.list_types()}"
            )
        return self._providers[key]

    def list_types(self) -> list[str]:
        """Return public provider names."""
        public = ["openai", "anthropic", "azure_openai", "gemini", "ollama", "generic"]
        return [
            item
            for item in public
            if self.is_supported(item) and self.get(item).model_class is not None
        ]

    def model_classes(self) -> Dict[str, Optional[Type[BaseModel]]]:
        """Return normalized provider class mapping for compatibility."""
        mapping = {key: spec.model_class for key, spec in self._providers.items()}
        for alias, key in self._aliases.items():
            mapping[alias] = self._providers[key].model_class
        return mapping

    def is_supported(self, api_type: Union[str, Any]) -> bool:
        return self.normalize_type(api_type) in self._aliases

    def get_model_class(self, api_type: Union[str, Any]) -> Type[BaseModel]:
        spec = self.get(api_type)
        if spec.model_class is None:
            raise ImportError(self._missing_provider_message(spec))
        return spec.model_class

    def create_model(
        self,
        api_type: Union[str, Any],
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> BaseModel:
        spec = self.get(api_type)
        model_class = self.get_model_class(api_type)

        if spec.key == "anthropic" and not is_anthropic_available():
            raise ImportError(self._missing_provider_message(spec))

        init_kwargs = dict(kwargs)
        if model_name is None:
            model_name = (
                init_kwargs.pop("model", None)
                or init_kwargs.get("model_name")
                or spec.default_model_name
            )
        init_kwargs.pop("model_name", None)

        if spec.key == "azure":
            azure_endpoint = (
                base_url
                or init_kwargs.pop("azure_endpoint", None)
                or init_kwargs.pop("base_url", None)
            )
            if not azure_endpoint:
                raise ValueError("Azure OpenAI 需要提供 base_url 或 azure_endpoint")
            deployment_name = init_kwargs.pop("azure_deployment", None) or model_name
            if not deployment_name:
                raise ValueError("Azure OpenAI 需要提供 model_name 或 azure_deployment")
            api_version = (
                init_kwargs.pop("azure_api_version", None)
                or init_kwargs.pop("api_version", None)
                or "2024-02-01"
            )
            return model_class(
                model_name=deployment_name,
                api_key=api_key,
                azure_endpoint=azure_endpoint,
                api_version=api_version,
                temperature=temperature,
                max_tokens=init_kwargs.pop("max_tokens", None),
                **init_kwargs,
            )

        resolved_base_url = base_url or init_kwargs.pop("base_url", None) or spec.default_base_url
        if spec.requires_base_url and not resolved_base_url:
            raise ValueError(f"{spec.display_name} 需要提供 base_url")

        return model_class(
            model_name=model_name,
            base_url=resolved_base_url,
            api_key=api_key,
            temperature=temperature,
            **init_kwargs,
        )

    def create_from_config(self, config: Dict[str, Any]) -> BaseModel:
        config_dict = _normalize_config(config)
        return self.create_model(
            api_type=config_dict.get("api_type", "openai"),
            model_name=config_dict.get("model_name") or config_dict.get("model"),
            base_url=config_dict.get("base_url"),
            api_key=config_dict.get("api_key"),
            temperature=config_dict.get("temperature", 0.2),
            **{
                key: value
                for key, value in config_dict.items()
                if key not in {"api_type", "model_name", "model", "base_url", "api_key", "temperature"}
            },
        )

    def validate_profile(
        self,
        api_type: Union[str, Any],
        model_name: Optional[str],
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        context_window: Optional[Any] = None,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        """Validate model profile shape without making a network call."""
        try:
            spec = self.get(api_type)
            self.get_model_class(api_type)
        except (ValueError, ImportError) as exc:
            return False, str(exc)

        if not str(model_name or "").strip() and not kwargs.get("azure_deployment"):
            return False, "模型名称不能为空"

        resolved_base_url = (
            base_url
            or kwargs.get("base_url")
            or kwargs.get("azure_endpoint")
            or spec.default_base_url
        )
        if spec.requires_base_url and not resolved_base_url:
            return False, f"{spec.display_name} 需要提供 base_url"

        if context_window is not None and not parse_context_window(context_window):
            return False, "模型上下文长度必须是正整数，支持纯数字、256k、1M 等格式"

        return True, ""

    def detect_context_window(
        self,
        api_type: Union[str, Any],
        model_name: str,
        **kwargs: Any,
    ) -> Optional[int]:
        """Create a model and ask the provider/API for an exact context window."""
        model = self.create_model(api_type, model_name=model_name, **kwargs)
        return model.detect_context_window()

    def get_model_info(self, api_type: Union[str, Any]) -> Dict[str, Any]:
        spec = self.get(api_type)
        return {
            "name": spec.display_name,
            "default_url": spec.default_base_url,
            "default_model": spec.default_model_name,
            "requires_api_key": spec.requires_api_key,
            "env_var": spec.env_var,
            "requires_package": spec.requires_package,
        }

    def test_connection(
        self,
        api_type: str,
        model_name: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        try:
            model = self.create_model(
                api_type=api_type,
                model_name=model_name,
                base_url=base_url,
                api_key=api_key,
                **kwargs,
            )
            return (True, "连接成功") if model.check_connection() else (False, "连接失败")
        except ImportError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"测试失败: {exc}"

    def _missing_provider_message(self, spec: ModelProviderSpec) -> str:
        if spec.requires_package:
            return f"使用 {spec.display_name} 需要安装 {spec.requires_package}。"
        return f"模型类型 '{spec.key}' 的依赖模块未安装。"


def _normalize_config(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        return dict(config)
    if hasattr(config, "to_dict"):
        return dict(config.to_dict())
    return {
        key: value
        for key, value in vars(config).items()
        if not key.startswith("_")
    }


def _build_default_registry() -> ModelProviderRegistry:
    registry = ModelProviderRegistry()
    openai = provider_catalog_entry("openai")
    registry.register(ModelProviderSpec(
        key="openai",
        model_class=OpenAIModel,
        display_name=openai.label,
        default_base_url=openai.runtime_default_base_url(),
        default_model_name=openai.default_model_name,
        requires_api_key=openai.requires_api_key,
        env_var=openai.api_key_env,
    ))
    anthropic = provider_catalog_entry("anthropic")
    registry.register(ModelProviderSpec(
        key="anthropic",
        model_class=AnthropicModel,
        display_name=anthropic.label,
        default_base_url=anthropic.runtime_default_base_url(),
        default_model_name=anthropic.default_model_name,
        requires_api_key=anthropic.requires_api_key,
        env_var=anthropic.api_key_env,
        requires_package=anthropic.requires_package,
    ))
    azure = provider_catalog_entry("azure_openai")
    registry.register(ModelProviderSpec(
        key="azure",
        model_class=AzureOpenAIModel,
        display_name=azure.label,
        aliases=("azure_openai",),
        default_base_url=azure.runtime_default_base_url(),
        default_model_name=azure.default_model_name,
        requires_api_key=azure.requires_api_key,
        env_var=azure.api_key_env,
        requires_base_url=azure.requires_base_url,
    ))
    gemini = provider_catalog_entry("gemini")
    registry.register(ModelProviderSpec(
        key="gemini",
        model_class=GeminiModel,
        display_name=gemini.label,
        default_base_url=gemini.runtime_default_base_url(),
        default_model_name=gemini.default_model_name,
        requires_api_key=gemini.requires_api_key,
        env_var=gemini.api_key_env,
    ))
    ollama = provider_catalog_entry("ollama")
    registry.register(ModelProviderSpec(
        key="ollama",
        model_class=OllamaModel,
        display_name=ollama.label,
        default_base_url=ollama.runtime_default_base_url(),
        default_model_name=ollama.default_model_name,
        requires_api_key=ollama.requires_api_key,
        requires_package=ollama.requires_package,
    ))
    generic = provider_catalog_entry("generic")
    registry.register(ModelProviderSpec(
        key="generic",
        model_class=GenericOpenAIModel or OpenAIModel,
        display_name=generic.label,
        default_base_url=generic.runtime_default_base_url(),
        default_model_name=generic.default_model_name,
        requires_api_key=generic.requires_api_key,
        env_var=generic.api_key_env,
        requires_base_url=generic.requires_base_url,
    ))
    return registry


DEFAULT_MODEL_PROVIDER_REGISTRY = _build_default_registry()


def get_model_provider_registry() -> ModelProviderRegistry:
    """Return the process default model provider registry."""
    return DEFAULT_MODEL_PROVIDER_REGISTRY


__all__ = [
    "DEFAULT_MODEL_PROVIDER_REGISTRY",
    "ModelProviderRegistry",
    "ModelProviderSpec",
    "get_model_provider_registry",
    "is_anthropic_available",
]
