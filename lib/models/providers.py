"""各 wire 协议的薄模型类 —— 直接继承 LangChain 官方集成。

**这里没有门面包装。** 每个类同时是：

* 一个真正的 LangChain chat model（``create_agent`` / ``bind_tools`` / ``invoke`` /
  ``stream`` 直接可用，无需委托）；
* 一个 :class:`~lib.models.extras.ModelExtras`（``context_window`` / 用量 / ``chat`` /
  ``chat_stream`` 等契约成员可用）。

每个协议类只声明**一个** ``WIRE_PROTOCOL``；其余差异（``model_type`` / ``provider`` /
各集成不一致的字段名）查 :data:`PROTOCOL_SPECS` 表，因此新增协议是在表里加一行，
而不是再写一个类。

可选依赖一律在 builder 里 ``try/except`` 保护：缺包时 ``resolve_protocol_class``
返回 ``None``，``import lib`` 仍然可用，注册表据此把该 provider 排除在
``list_types()`` 之外。

**为什么连 langchain-openai（硬依赖）也延迟。** 它是全部 SDK 里 import 最慢的
（约 5 秒，大头在 azure 子模块），而一次 CLI 启动只用得到当前 profile 的那一个
协议。类定义搬进 builder 之后，``import lib.models.providers`` 不再拖任何厂商
SDK——按需解析，缺哪个才付哪个的钱。

**类级声明必须用 ``ClassVar`` 且不带下划线前缀**：本模块的类是 pydantic 模型，
pydantic 要求非下划线类属性注解为 ClassVar，而下划线属性会被当成私有属性接管
（子类覆盖会静默失效 —— 实测子类值读出来仍是基类默认）。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, ClassVar, Optional

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
    """协议类公共部分：按 :data:`PROTOCOL_SPECS` 取字段名与元信息。

    刻意**不**在本类提供 ``model_name`` / ``base_url`` 属性：部分集成已经用这些名字
    承载自己的字段（``ChatOllama.base_url``、``ChatOpenAI.model_name``），在此覆盖会
    破坏它们。缺失的访问器由具体协议类按需补齐。
    """

    WIRE_PROTOCOL: ClassVar[str] = ""

    def __init__(self, **data: Any) -> None:
        """在交给 pydantic 之前消化掉本层私有参数。

        **为什么必须在构造入口做，而不是靠字段声明或构造后赋值。**
        ``context_window`` 是 :class:`~lib.models.extras.ModelExtras` 上的属性（值存在
        ``self.__dict__`` 里），**不是** LangChain 字段。若把它原样传进构造函数，
        LangChain 的 ``_build_model_kwargs`` 会判定它「不是默认参数」、收进
        ``model_kwargs``，进而**原样发进请求体** —— 实测请求体里真的多出
        ``{"context_window": 12345}``，厂商会以未知参数为由拒绝整个请求。

        ``model_name`` 同理：除 ``ChatOpenAI`` 外各集成的字段名都是 ``model``，
        多余的 ``model_name`` 也会被当成未知参数送进 ``model_kwargs``。

        工厂路径（:meth:`~lib.models.registry.ModelProviderRegistry.create_model`）
        本来就会过滤这些键，但协议类本身是对外公开、且文档写着「可直接使用」的，
        因此这条不变量的守卫必须落在类自己身上，否则就只是「顺风路径上的守卫」。
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
        """``model_name=`` 兜底 —— ``__init__`` 之外的第二条入口。

        ``__init__`` 已覆盖正常构造路径；本校验器负责绕过 ``__init__`` 的路径
        （``model_validate`` / ``model_construct``）。LangChain 各集成的字段名并不一致：
        ``ChatOpenAI`` 有 ``model_name``，其余只有 ``model``；不兜住的话
        ``GeminiModel(model_name=...)`` 会因缺少必填的 ``model`` 而报错。
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
        """取出密钥**明文**，供上下文窗口探测使用。

        **为什么必须解包。** 厂商集成把密钥字段声明成 pydantic ``SecretStr``，而
        ``str(SecretStr(...))`` 返回的是 ``'**********'`` 掩码，不是密钥本身。把掩码
        当凭据发出去，探测请求会以 **401** 失败；而探测函数对所有非 200 一律返回
        ``None``（设计如此：探测失败即「未知」），于是表现为**上下文窗口永远探测不到**，
        且完全无声。实测真实端点：掩码 → 401，明文 → 200。

        这是一次重写引入的回归：旧实现把密钥存成普通 ``str``，直接拼进
        ``Authorization`` 头，因此是正确的。
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


# ==============================================================================
# 协议类 builder：每个 builder 按需 import 对应 SDK 并定义类。
# 缺包时返回 None（与过去顶层 try/except 的降级值完全一致）。
# ==============================================================================


def _build_openai_model() -> Any:
    from langchain_openai import ChatOpenAI

    class OpenAIModel(NonstandardPassthroughMixin, _ProtocolModel, ChatOpenAI):
        """OpenAI 及任意 OpenAI 兼容端点（``protocol=openai``）。"""

        WIRE_PROTOCOL: ClassVar[str] = "openai"

        # 默认兼容开关（直接构造时生效）；经工厂创建时由目录条目覆盖。
        # 这里刻意与目录里的 ``_OPENAI_COMPATIBLE`` 保持一致，避免两条路径行为不同。
        compat: CompatSwitches = CompatSwitches(passthrough_nonstandard=True)

        @property
        def base_url(self) -> str:
            return str(self.openai_api_base or "")

    return OpenAIModel


def _build_azure_model() -> Any:
    from langchain_openai import AzureChatOpenAI

    class AzureOpenAIModel(_ProtocolModel, AzureChatOpenAI):
        """Azure OpenAI 部署（``protocol=azure_openai``）。"""

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
        """DeepSeek 官方 API（``protocol=deepseek``）。

        推理内容走 ``reasoning_content``，需要响应提取 + 请求回填双向透传 —— 这正是
        ``NonstandardPassthroughMixin`` 的职责，不再需要独立的 DeepSeek 子类。
        该字段已在 ``compat.py`` 的内置已知集合里，因此开关组合与其它
        OpenAI 兼容端点完全相同。
        """

        WIRE_PROTOCOL: ClassVar[str] = "deepseek"

        compat: CompatSwitches = CompatSwitches(passthrough_nonstandard=True)

        @property
        def base_url(self) -> str:
            return str(self.openai_api_base or self.api_base or "")

    return DeepSeekModel


# ==============================================================================
# 其他协议
# ==============================================================================


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
        """Anthropic Claude（``protocol=anthropic``）。"""

        WIRE_PROTOCOL: ClassVar[str] = "anthropic"

        @property
        def model_name(self) -> str:
            # ChatAnthropic 的字段名是 model（model_name 只是别名，无属性）。
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
        """本地 Ollama 服务（``protocol=ollama``）。"""

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
        """Google Gemini（``protocol=gemini``，走官方集成而非手写 REST）。"""

        WIRE_PROTOCOL: ClassVar[str] = "gemini"

        @property
        def model_name(self) -> str:
            return str(self.model)

    return GeminiModel


# 协议名 → builder。工厂据此把目录里的 protocol 解析成可实例化的类，
# 用到哪个协议才 import 哪个 SDK（带缓存，见 resolve_protocol_class）。
_PROTOCOL_BUILDERS: dict[str, Any] = {
    "openai": _build_openai_model,
    "azure_openai": _build_azure_model,
    "deepseek": _build_deepseek_model,
    "anthropic": _build_anthropic_model,
    "ollama": _build_ollama_model,
    "gemini": _build_gemini_model,
}

# 协议类名 → 协议名（模块 __getattr__ 用，保持 from .providers import X 可用）。
_PROTOCOL_CLASS_NAMES: dict[str, str] = {
    "OpenAIModel": "openai",
    "AzureOpenAIModel": "azure_openai",
    "DeepSeekModel": "deepseek",
    "AnthropicModel": "anthropic",
    "OllamaModel": "ollama",
    "GeminiModel": "gemini",
}

_PROTOCOL_CLASS_CACHE: dict[str, Any] = {}


def resolve_protocol_class(protocol: str) -> Any:
    """按需解析协议类：第一次用到才 import 对应 SDK，缺包返回 None。

    与过去顶层 try/except 的降级值完全一致（缺包 → None），只是时机从
    import 期推迟到首次使用——``import lib.models.providers`` 从此不拖任何厂商 SDK。
    """
    if protocol in _PROTOCOL_CLASS_CACHE:
        return _PROTOCOL_CLASS_CACHE[protocol]
    builder = _PROTOCOL_BUILDERS.get(protocol)
    cls = builder() if builder is not None else None
    _PROTOCOL_CLASS_CACHE[protocol] = cls
    return cls


class _LazyProtocolMap:
    """``PROTOCOL_CLASSES`` 的惰性外壳：读操作按需解析，行为与旧 dict 一致。

    ``.get()`` / ``[]`` / ``in`` 都可用；迭代只列协议名（不触发解析，
    否则"列个表"也要拖六个 SDK 进来）。
    """

    def __getitem__(self, protocol: str) -> Any:
        cls = resolve_protocol_class(protocol)
        if cls is None:
            raise KeyError(protocol)
        return cls

    def get(self, protocol: str, default: Any = None) -> Any:
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
    """PEP 562：``from .providers import OpenAIModel`` 首次访问时才解析。"""
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
