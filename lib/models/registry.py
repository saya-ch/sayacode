"""模型 provider 注册表 —— 完全由 :mod:`.provider_catalog` 驱动。

注册表本身不含任何 provider 事实：默认 spec 是对目录的一次遍历，协议到模型类的
解析走 :func:`~lib.models.providers.resolve_protocol_class`（**按需**，不是 import 期）。
因此：

* 在目录里加一条 :class:`~lib.models.provider_catalog.ProviderCatalogEntry`，
  provider 就自动出现在 ``list_types()`` / 配置界面 / 校验里；
* 在 :data:`PROTOCOL_SPECS` 里加一个协议，新的 wire 协议就被支持。

刻意**没有** provider 专属分支：Azure 的认证参数、DeepSeek 的推理字段都通过
目录里的 ``protocol`` 与 ``compat`` 表达（见 :mod:`.providers` 与 :mod:`.compat`）。

惰性说明：``_build_default_registry()`` 只登记协议名，不解析模型类——否则
``import lib.models.registry`` 会拖进全部六个厂商 SDK（约 12 秒）。
``get_model_class()`` 首次用到才解析；``list_types()`` 只做包存在性探测
（``find_spec``，不 import），缺包的 provider 照样被排除。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, Dict, Iterable, Optional, Tuple, Type, Union

from . import providers as _providers_module
from .provider_catalog import (
    PROVIDER_CATALOG,
    normalize_provider_type,
    provider_catalog_entry,
)
from .providers import is_anthropic_available, is_ollama_available
from .vocabulary import parse_context_window


@dataclass(frozen=True)
class ModelProviderSpec:
    """一个已注册的模型 provider。"""

    key: str
    model_class: Optional[Type[Any]]
    display_name: str

    # wire 协议名；决定 model_class 与请求构造方式。
    protocol: str = ""

    aliases: Tuple[str, ...] = ()
    default_base_url: Optional[str] = None
    default_model_name: Optional[str] = None
    requires_api_key: bool = False
    env_var: Optional[str] = None
    requires_package: Optional[str] = None
    requires_base_url: bool = False


class ModelProviderRegistry:
    """通过单一注册表创建和查看模型 provider。"""

    def __init__(self, providers: Optional[Iterable[ModelProviderSpec]] = None) -> None:
        self._providers: Dict[str, ModelProviderSpec] = {}
        self._aliases: Dict[str, str] = {}
        for provider in providers or ():
            self.register(provider)

    def register(self, provider: ModelProviderSpec) -> None:
        """登记单个 provider 规格。"""
        key = self.normalize_type(provider.key)
        normalized = ModelProviderSpec(
            key=key,
            model_class=provider.model_class,
            display_name=provider.display_name,
            protocol=provider.protocol,
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
        """归一化类型名，解析目录里声明的别名（如 ``azure`` → ``azure_openai``）。"""
        return normalize_provider_type(api_type)

    def get(self, api_type: Union[str, Any]) -> ModelProviderSpec:
        """按名称取 provider 规格。"""
        key = self._aliases.get(self.normalize_type(api_type))
        if not key or key not in self._providers:
            raise ValueError(
                f"不支持的模型类型: {api_type}。"
                f"支持的类型: {self.list_types()}"
            )
        return self._providers[key]

    def list_types(self) -> list[str]:
        """返回公开的 provider 名称（缺包的不在内，且不为此 import 任何 SDK）。"""
        return [
            key
            for key in PROVIDER_CATALOG
            if self.is_supported(key) and self._spec_available(self.get(key))
        ]

    @staticmethod
    def _spec_available(spec: "ModelProviderSpec") -> bool:
        if spec.model_class is not None:
            return True
        return _package_available(spec.requires_package)

    def model_classes(self) -> Dict[str, Optional[Type[Any]]]:
        """返回归一化后的 provider 类映射，用于兼容性。"""
        mapping = {key: spec.model_class for key, spec in self._providers.items()}
        for alias, key in self._aliases.items():
            mapping[alias] = self._providers[key].model_class
        return mapping

    def is_supported(self, api_type: Union[str, Any]) -> bool:
        """判断 provider 是否已注册。"""
        return self.normalize_type(api_type) in self._aliases

    def get_model_class(self, api_type: Union[str, Any]) -> Type[Any]:
        """首次用到才解析协议类；缺包时抛指明包名的 ImportError。

        注意经模块属性调用（而不是 import 期绑名字），否则单测无法模拟缺包。
        """
        spec = self.get(api_type)
        model_class = spec.model_class or _providers_module.resolve_protocol_class(spec.protocol)
        if model_class is None:
            raise ImportError(self._missing_provider_message(spec))
        return model_class

    def create_model(
        self,
        api_type: Union[str, Any],
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> Any:
        """按目录声明实例化模型。

        各 LangChain 集成的构造参数名并不一致，但 ``model`` / ``api_key`` /
        ``base_url`` 是它们共同的别名，因此默认路径是统一的；Azure 因为用端点 +
        部署名 + API 版本认证，需要单独的字段映射。

        ``context_window`` 不需要在此特殊处理：它由
        :meth:`lib.models.providers._ProtocolModel.__init__` 在构造入口消化掉
        （既不会丢，也不会漏进请求体），本方法只负责把它原样传下去。
        """
        spec = self.get(api_type)
        model_class = self.get_model_class(api_type)

        if spec.key == "anthropic" and not is_anthropic_available():
            raise ImportError(self._missing_provider_message(spec))

        # 丢弃取值为 None 的额外参数：它们表示「未设置」，但会被上游放进
        # ``model_kwargs`` 并**原样发进请求体**。实测一个带 ``azure_api_version: None``
        # 的已保存 profile 会让每次真实调用都以
        # ``TypeError: Completions.create() got an unexpected keyword argument`` 失败
        # —— 这是 mock 测试完全测不出来的问题。
        init_kwargs = {key: value for key, value in kwargs.items() if value is not None}

        # ``model`` 与 ``model_name`` 是同一个东西的两个名字。显式参数优先；
        # 无论走哪个分支都要把它从 init_kwargs 里摘掉，否则会与下面显式传入的
        # ``model=`` 撞成「got multiple values for keyword argument 'model'」。
        model_from_kwargs = init_kwargs.pop("model", None)
        if model_name is None:
            model_name = model_from_kwargs or spec.default_model_name

        entry = provider_catalog_entry(spec.key)

        if spec.protocol == "azure_openai":
            azure_endpoint = (
                base_url
                or init_kwargs.pop("azure_endpoint", None)
                or init_kwargs.pop("base_url", None)
            )
            if not azure_endpoint:
                raise ValueError("Azure OpenAI 需要提供 base_url 或 azure_endpoint")
            api_version = (
                init_kwargs.pop("azure_api_version", None)
                or init_kwargs.pop("api_version", None)
                or "2024-02-01"
            )
            model = model_class(
                model=model_name,
                api_key=api_key,
                azure_endpoint=azure_endpoint,
                api_version=api_version,
                temperature=temperature,
                **init_kwargs,
            )
        else:
            # Azure 专属键对其它协议没有意义；留着会被当成请求体参数发给厂商。
            for azure_key in ("azure_endpoint", "azure_deployment", "azure_api_version", "api_version"):
                init_kwargs.pop(azure_key, None)

            resolved_base_url = base_url or init_kwargs.pop("base_url", None) or spec.default_base_url
            if spec.requires_base_url and not resolved_base_url:
                raise ValueError(f"{spec.display_name} 需要提供 base_url")

            model = model_class(
                model=model_name,
                api_key=api_key,
                base_url=resolved_base_url,
                temperature=temperature,
                **init_kwargs,
            )

        # 兼容开关只在声明了该字段的协议类上注入（openai / deepseek 的透传 mixin）。
        # 注入发生在构造之后，因此工厂给的取值**总是**覆盖构造参数里的默认值 ——
        # 目录是单一事实来源，直接构造时才有自由。
        if "compat" in getattr(model_class, "model_fields", {}):
            model.compat = entry.compat

        return model

    def create_from_config(self, config: Dict[str, Any]) -> Any:
        """从配置字典创建模型实例。"""
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
                if key not in _CONFIG_ONLY_KEYS
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
    ) -> Tuple[bool, str]:
        """在不发起网络请求的前提下校验模型 profile 结构。"""
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

        resolved_api_key = (
            api_key
            if api_key is not None
            else kwargs.get("api_key")
        )
        if spec.requires_api_key and not str(resolved_api_key or "").strip():
            return False, f"{spec.display_name} 需要提供 api_key"

        if context_window is not None and not parse_context_window(context_window):
            return False, "模型上下文长度必须是正整数，支持纯数字、256k、1M 等格式"

        return True, ""

    def detect_context_window(
        self,
        api_type: Union[str, Any],
        model_name: str,
        **kwargs: Any,
    ) -> Optional[int]:
        """创建模型并向 provider/API 询问准确的 context window。"""
        model = self.create_model(api_type, model_name=model_name, **kwargs)
        return model.detect_context_window()

    def get_model_info(self, api_type: Union[str, Any]) -> Dict[str, Any]:
        """返回 provider 展示信息。"""
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
    ) -> Tuple[bool, str]:
        """创建模型并验证连通性。"""
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


# 属于 harness 配置层、而不是模型构造参数的键。
# 透传下去会被上游收进 ``model_kwargs``，进而原样出现在请求体里。
#
# ``context_window`` 刻意**不**在本集合里：它必须传下去，
# 由 ``_ProtocolModel.__init__`` 在构造入口消费（曾经误加进本集合，导致保存的
# 窗口值被静默丢弃；也曾经完全不处理，导致它漏进请求体）。
_CONFIG_ONLY_KEYS = frozenset({
    "api_type",
    "model_name",
    "model",
    "base_url",
    "api_key",
    "temperature",
    "metadata",
})


def _package_available(requires_package: Optional[str]) -> bool:
    """包存在性探测（不 import）：目录里 pip 名与模块名仅差连字符/下划线。"""
    if not requires_package:
        return True
    return find_spec(requires_package.replace("-", "_")) is not None


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
    """对 provider 目录的一次遍历 —— 没有逐 provider 的手写 spec。

    注意 ``model_class`` 刻意留 ``None``：解析推迟到 ``get_model_class()``
    首次调用。否则注册表 import 即拖进全部厂商 SDK，惰性化前功尽弃。
    """
    registry = ModelProviderRegistry()
    for key, entry in PROVIDER_CATALOG.items():
        registry.register(ModelProviderSpec(
            key=key,
            protocol=entry.protocol,
            model_class=None,
            display_name=entry.label,
            aliases=entry.aliases,
            default_base_url=entry.runtime_default_base_url(),
            default_model_name=entry.default_model_name,
            requires_api_key=entry.requires_api_key,
            env_var=entry.api_key_env,
            requires_package=entry.requires_package,
            requires_base_url=entry.requires_base_url,
        ))
    return registry


DEFAULT_MODEL_PROVIDER_REGISTRY = _build_default_registry()


def get_model_provider_registry() -> ModelProviderRegistry:
    """返回进程默认的模型 provider 注册表。"""
    return DEFAULT_MODEL_PROVIDER_REGISTRY


__all__ = [
    "DEFAULT_MODEL_PROVIDER_REGISTRY",
    "ModelProviderRegistry",
    "ModelProviderSpec",
    "get_model_provider_registry",
    "is_anthropic_available",
    "is_ollama_available",
]
