"""各协议薄模型类，直接继承官方集成。

每个类同时是可直接调用的聊天模型，也是满足内部契约的模型。
每个协议类只声明一个协议名，其余差异查协议差异表，新增协议加一行即可。
可选依赖在构造器里保护，缺包返回空，导入本模块不拖厂商包。
官方集成导入较慢，类定义放进构造器，按需解析，用到哪个才付哪个成本。
类级声明用不带下划线的类变量，否则会被模型基类接管，子类覆盖失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, Callable, ClassVar, Optional

from pydantic import model_validator

from .compat import NonstandardPassthroughMixin
from .extras import ModelExtras
from .probing import probe_context_window
from .provider_catalog import CompatSwitches
from .vocabulary import ModelInfo


def _has_package(name: str) -> bool:
    return find_spec(name) is not None


@dataclass(frozen=True)
class ProtocolSpec:
    """一个 wire 协议的差异声明。"""

    model_type: str
    provider: str

    # 各 LangChain 集成的字段名并不一致：ChatOpenAI 用 model_name/openai_api_base，
    # ChatAnthropic 用 model/anthropic_api_url，ChatOllama 用 model/base_url。
    model_field: str
    base_url_field: str
    api_key_field: Optional[str]


PROTOCOL_SPECS: dict[str, ProtocolSpec] = {
    "openai": ProtocolSpec("openai", "OpenAI Compatible", "model_name", "openai_api_base", "openai_api_key"),
    "azure_openai": ProtocolSpec("azure_openai", "Azure OpenAI", "model_name", "azure_endpoint", "openai_api_key"),
    "deepseek": ProtocolSpec("deepseek", "DeepSeek", "model_name", "openai_api_base", "api_key"),
    "anthropic": ProtocolSpec("anthropic", "Anthropic", "model", "anthropic_api_url", "anthropic_api_key"),
    "ollama": ProtocolSpec("ollama", "Ollama", "model", "base_url", None),
    "gemini": ProtocolSpec("gemini", "Google Gemini", "model", "base_url", "google_api_key"),
}


class _ProtocolModel(ModelExtras):
    """协议类公共部分，按协议差异表取字段名与元信息。

    不在本类提供模型名与地址属性，各集成已有同名字段，覆盖会破坏它们。
    缺失的访问器由具体协议类按需补齐。
    """

    WIRE_PROTOCOL: ClassVar[str] = ""

    def __init__(self, **data: Any) -> None:
        # 构造参数透传给 pydantic 与各家集成，键形态不一，保持 Any
        """在交给校验之前消化本层私有参数。

        上下文窗口不是上游字段，原样传入会被收进额外请求参数并发给厂商，
        厂商会以未知参数拒绝整个请求，模型名同理。
        工厂路径会过滤这些键，但协议类公开可直接使用，守卫必须落在类自己身上。
        """
        context_window = data.pop("context_window", None)

        if "model" in data:
            # 两个名字都给了：以 model 为准，丢弃 model_name 以免漏进 model_kwargs
            data.pop("model_name", None)
        elif "model_name" in data:
            data["model"] = data.pop("model_name")

        super().__init__(**data)

        if context_window is not None:
            self.context_window = context_window

    @model_validator(mode="before")
    @classmethod
    def _accept_model_name_kwarg(cls, data: Any) -> Any:
        # 校验器输入是外部扔进来的未知形态，保持 Any
        """模型名兜底，覆盖绕过构造函数的路径。

        正常构造已覆盖，本校验器负责直接校验与构造的路径。
        各集成字段名不一致，不兜住会因缺少必填模型名而报错。
        """
        if isinstance(data, dict) and "model" not in data and "model_name" in data:
            data = dict(data)
            data["model"] = data.pop("model_name")
        return data

    @property
    def protocol_spec(self) -> ProtocolSpec:
        return PROTOCOL_SPECS[self.WIRE_PROTOCOL]

    def _probe_api_for_context_window(self) -> Optional[int]:
        spec = self.protocol_spec
        return probe_context_window(
            self.WIRE_PROTOCOL,
            str(getattr(self, spec.base_url_field, "") or ""),
            str(getattr(self, spec.model_field, "") or ""),
            self._resolved_api_key(),
        )

    def _resolved_api_key(self) -> Optional[str]:
        """取出密钥明文，供上下文窗口探测使用。

        上游把密钥存成保密字符串，直接转字符串只得掩码。
        掩码当凭据会认证失败，探测函数对失败一律返回未知，且完全无声。
        因此必须解包后再用。
        """
        field = self.protocol_spec.api_key_field
        if field is None:
            return None
        value = getattr(self, field, None)
        if value is None:
            return None
        unwrap = getattr(value, "get_secret_value", None)
        if callable(unwrap):
            value = unwrap()
        return str(value) if value else None

    def get_model_info(self):
        """按协议声明返回模型元信息。"""
        return ModelInfo(
            name=str(getattr(self, "model_name", "")),
            model_type=self.protocol_spec.model_type,
            provider=self.protocol_spec.provider,
            supported_params=[],
            supports_streaming=self.SUPPORTS_STREAMING,
        )


# 协议类构造器，每个按需导入对应依赖并定义类。
# 缺包时返回空。


def _build_openai_model() -> Any:
    from langchain_openai import ChatOpenAI

    class OpenAIModel(NonstandardPassthroughMixin, _ProtocolModel, ChatOpenAI):
        """开放与兼容端点，协议名为开放协议。"""

        WIRE_PROTOCOL: ClassVar[str] = "openai"

        # 默认兼容开关（直接构造时生效）；经工厂创建时由目录条目覆盖。
        # 这里刻意与目录里的默认兼容开关保持一致，避免两条路径行为不同。
        compat: CompatSwitches = CompatSwitches(passthrough_nonstandard=True)

        @property
        def base_url(self) -> str:
            return str(self.openai_api_base or "")

    return OpenAIModel


def _build_azure_model() -> Any:
    from langchain_openai import AzureChatOpenAI

    class AzureOpenAIModel(_ProtocolModel, AzureChatOpenAI):
        """云端部署，协议名为云端协议。"""

        WIRE_PROTOCOL: ClassVar[str] = "azure_openai"

        @property
        def base_url(self) -> str:
            return str(self.azure_endpoint or "")

    return AzureOpenAIModel


def _build_deepseek_model() -> Any:
    try:
        from langchain_deepseek import ChatDeepSeek
    except ImportError:
        return None

    class DeepSeekModel(NonstandardPassthroughMixin, _ProtocolModel, ChatDeepSeek):
        """官方接口，推理字段双向透传，已在已知集合声明，开关与其他兼容端点相同。"""

        WIRE_PROTOCOL: ClassVar[str] = "deepseek"

        compat: CompatSwitches = CompatSwitches(passthrough_nonstandard=True)

        @property
        def base_url(self) -> str:
            return str(self.openai_api_base or self.api_base or "")

    return DeepSeekModel


# 其他协议


def is_anthropic_available() -> bool:
    """Anthropic 集成是否可用（依赖已安装）。"""
    return _has_package("langchain_anthropic")


def is_ollama_available() -> bool:
    """Ollama 集成是否可用（依赖已安装）。"""
    return _has_package("langchain_ollama")


def _build_anthropic_model() -> Any:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        return None

    class AnthropicModel(_ProtocolModel, ChatAnthropic):
        """对话模型，协议名为对话协议。"""

        WIRE_PROTOCOL: ClassVar[str] = "anthropic"

        @property
        def model_name(self) -> str:
            # 上游字段名是模型字段，模型名只是别名，无独立属性。
            return str(self.model)

        @property
        def base_url(self) -> str:
            return str(self.anthropic_api_url or "")

    return AnthropicModel


def _build_ollama_model() -> Any:
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        return None

    class OllamaModel(_ProtocolModel, ChatOllama):
        """本地服务，协议名为本地协议。"""

        WIRE_PROTOCOL: ClassVar[str] = "ollama"

        @property
        def model_name(self) -> str:
            return str(self.model)

    return OllamaModel


def _build_gemini_model() -> Any:
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        return None

    class GeminiModel(_ProtocolModel, ChatGoogleGenerativeAI):
        """生成模型，走官方集成，协议名为生成协议。"""

        WIRE_PROTOCOL: ClassVar[str] = "gemini"

        @property
        def model_name(self) -> str:
            return str(self.model)

    return GeminiModel


# 协议名到构造器，用到哪个协议才导入哪个依赖，结果带缓存。
# 协议名到构建函数，缺包时返回空
_PROTOCOL_BUILDERS: dict[str, Callable[[], Any]] = {
    "openai": _build_openai_model,
    "azure_openai": _build_azure_model,
    "deepseek": _build_deepseek_model,
    "anthropic": _build_anthropic_model,
    "ollama": _build_ollama_model,
    "gemini": _build_gemini_model,
}

# 协议类名到协议名，模块懒解析用，保持导入写法可用。
_PROTOCOL_CLASS_NAMES: dict[str, str] = {
    "OpenAIModel": "openai",
    "AzureOpenAIModel": "azure_openai",
    "DeepSeekModel": "deepseek",
    "AnthropicModel": "anthropic",
    "OllamaModel": "ollama",
    "GeminiModel": "gemini",
}

# 缓存值是动态构建的协议类或缺包时的 None，形态不统一，保持 Any
_PROTOCOL_CLASS_CACHE: dict[str, Any] = {}


def resolve_protocol_class(protocol: str) -> Any:
    """按需解析协议类，首次用到才导入对应依赖，缺包返回空。

    缺包与旧降级值一致，只是时机从导入期推迟到首次使用。
    """
    if protocol in _PROTOCOL_CLASS_CACHE:
        return _PROTOCOL_CLASS_CACHE[protocol]
    builder = _PROTOCOL_BUILDERS.get(protocol)
    cls = builder() if builder is not None else None
    _PROTOCOL_CLASS_CACHE[protocol] = cls
    return cls


class _LazyProtocolMap:
    """协议表的懒外壳，读操作按需解析，行为与字典一致。

    取值与判断都可用，迭代只列协议名，不触发解析。
    """

    def __getitem__(self, protocol: str) -> Any:
        # 返回动态解析的协议类，各家类型不统一，保持 Any
        cls = resolve_protocol_class(protocol)
        if cls is None:
            raise KeyError(protocol)
        return cls

    def get(self, protocol: str, default: Any = None) -> Any:
        # 返回动态解析的协议类或调用方给的默认值，形态不统一，保持 Any
        cls = resolve_protocol_class(protocol)
        return cls if cls is not None else default

    def __contains__(self, protocol: object) -> bool:
        return isinstance(protocol, str) and protocol in _PROTOCOL_BUILDERS

    def __iter__(self):
        return iter(_PROTOCOL_BUILDERS)

    def __len__(self) -> int:
        return len(_PROTOCOL_BUILDERS)


PROTOCOL_CLASSES = _LazyProtocolMap()


def __getattr__(name: str) -> Any:
    # 返回动态解析的协议类，各家类型不统一，保持 Any
    """模块懒解析，首次访问协议类名时才解析。"""
    if name in _PROTOCOL_CLASS_NAMES:
        return resolve_protocol_class(_PROTOCOL_CLASS_NAMES[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # 协议类经模块 __getattr__ 惰性解析（noqa: F822 —— 静态看不到不等于不存在）。
    "AnthropicModel",  # noqa: F822
    "AzureOpenAIModel",  # noqa: F822
    "DeepSeekModel",  # noqa: F822
    "GeminiModel",  # noqa: F822
    "OllamaModel",  # noqa: F822
    "OpenAIModel",  # noqa: F822
    "PROTOCOL_CLASSES",
    "PROTOCOL_SPECS",
    "ProtocolSpec",
    "is_anthropic_available",
    "is_ollama_available",
    "resolve_protocol_class",
]
