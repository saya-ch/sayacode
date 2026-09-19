"""模型 provider 目录 —— 声明式纯数据。

本模块是**唯一**的 provider 事实来源：端点、默认模型、凭据环境变量、wire 协议
与兼容开关全部声明在这里，运行时层与配置 UI 只读取、不重声明
（由 ``tests/test_architecture_boundaries.py`` 的门禁保证）。

新增一个 provider 通常只需要在这里加一条 ``ProviderCatalogEntry``：

* 目录里已描述过的厂商 → 填 ``protocol`` 与凭据，用默认 ``compat``；
* 目录未描述过的 OpenAI 兼容端点 → ``protocol="openai"`` +
  ``compat=CompatSwitches(passthrough_nonstandard=True)``，**无需新代码**。

字段分三类，勿混淆：

1. **被工厂消费**：``protocol``（决定用哪个 LangChain 集成类）、
   ``default_base_url``、``default_model_name``、``api_key_env``、
   ``requires_api_key``、``requires_base_url``、``requires_package``、
   ``base_url_env``、``compat``、``aliases``。
2. **被 UI/profile 消费**：``label``、``description``、``endpoint``、``visible``、``models``。
3. ``value`` 是键自身的规范化拷贝，供 dataclass 携带自身标识。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Optional


@dataclass(frozen=True)
class CompatSwitches:
    """针对「OpenAI 兼容但不完全兼容」端点的声明式开关。

    ``OpenAI 兼容`` 从来不是「完全兼容」：system prompt 放哪个 role、输出上限用哪个
    字段、厂商特有字段怎么在线上表达，各家都可能不同。这些差异**用数据表达**，
    而不是为每个厂商写一份适配代码。

    五个开关都**真被读取**（由 ``lib.models.compat`` 消费），不是描述性字段：

    * ``passthrough_nonstandard`` / ``extra_passthrough_fields`` →
      :class:`~lib.models.compat.NonstandardPassthroughMixin`；
    * ``system_role`` / ``max_tokens_field`` / ``supports_max_output_tokens`` →
      :func:`~lib.models.compat.apply_compat_to_payload`。
    """

    # 非标准字段透传的**总闸**：控制 NonstandardPassthroughMixin 是否在响应与请求
    # 之间搬运厂商特有字段（``reasoning_content`` / ``citations`` 等，
    # langchain-openai 默认会丢弃它们）。关闭时该 mixin 完全不动作。
    passthrough_nonstandard: bool = False

    # 内置已知字段集合之外需要一并透传的字段名，提取与回填两个方向都生效
    # 新端点有特有字段时在这里加名字即可，不用改代码
    extra_passthrough_fields: tuple[str, ...] = ()

    # system prompt 走哪个 role（少数网关要求 ``"developer"`` 或折进 user）。
    system_role: str = "system"

    # 输出上限对应的请求字段名（``max_tokens`` / ``max_completion_tokens``）。
    max_tokens_field: str = "max_tokens"

    # 该端点是否接受 max_tokens 类字段；False 时请求里省略。
    supports_max_output_tokens: bool = True


@dataclass(frozen=True)
class ProviderCatalogEntry:
    """静态模型 provider 元数据。"""

    value: str
    label: str
    description: str
    default_base_url: str
    default_model_name: str
    api_key_env: Optional[str]
    requires_api_key: bool
    endpoint: str

    # LangChain 集成键，决定工厂实例化哪个 chat model 类。
    protocol: str

    aliases: tuple[str, ...] = ()
    requires_base_url: bool = False
    requires_package: Optional[str] = None
    visible: bool = True
    base_url_env: Optional[str] = None

    # 该 provider 下已知可用的模型名，供配置界面提示选择，可被用户覆盖
    models: tuple[str, ...] = ()

    # 端点兼容性开关。默认值即「标准行为」。
    compat: CompatSwitches = field(default_factory=CompatSwitches)

    def resolved_default_base_url(self) -> str:
        """解析默认 base_url（含环境变量覆盖）。"""
        if self.base_url_env:
            return os.environ.get(self.base_url_env, self.default_base_url)
        return self.default_base_url

    def runtime_default_base_url(self) -> Optional[str]:
        """返回运行时默认 base_url。"""
        if self.requires_base_url:
            return None
        return self.resolved_default_base_url()


# OpenAI 兼容端点的通行兼容组合：保留厂商特有字段。
_OPENAI_COMPATIBLE = CompatSwitches(passthrough_nonstandard=True)


PROVIDER_CATALOG: dict[str, ProviderCatalogEntry] = {
    "openai": ProviderCatalogEntry(
        value="openai",
        label="OpenAI",
        description="Hosted and OpenAI-compatible APIs",
        default_base_url="https://api.openai.com/v1",
        default_model_name="gpt-4",
        api_key_env="OPENAI_API_KEY",
        requires_api_key=True,
        endpoint="/v1/chat/completions",
        protocol="openai",
        models=("gpt-4", "gpt-4o", "gpt-4o-mini"),
        compat=_OPENAI_COMPATIBLE,
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
        protocol="anthropic",
        requires_package="langchain-anthropic",
        models=("claude-sonnet-4-20250514",),
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
        protocol="azure_openai",
        aliases=("azure",),
        requires_base_url=True,
        requires_package="langchain-openai",
        visible=False,
        models=("gpt-4",),
    ),
    "deepseek": ProviderCatalogEntry(
        value="deepseek",
        label="DeepSeek",
        description="DeepSeek official chat-completions API",
        default_base_url="https://api.deepseek.com/v1",
        default_model_name="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        requires_api_key=True,
        endpoint="/v1/chat/completions",
        protocol="deepseek",
        requires_package="langchain-deepseek",
        models=("deepseek-chat", "deepseek-reasoner"),
        # DeepSeek 的推理内容走 reasoning_content，需要双向透传 —— 该字段已在
        # compat.py 的内置已知集合里，因此这里用与其它 OpenAI 兼容端点相同的组合。
        compat=_OPENAI_COMPATIBLE,
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
        protocol="gemini",
        requires_package="langchain-google-genai",
        models=("gemini-2.5-flash", "gemini-2.5-pro"),
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
        protocol="ollama",
        requires_package="langchain-ollama",
        base_url_env="OLLAMA_BASE_URL",
        models=("qwen3.5:9b",),
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
        protocol="openai",
        requires_base_url=True,
        visible=False,
        compat=_OPENAI_COMPATIBLE,
    ),
}

USER_VISIBLE_PROVIDER_TYPES = tuple(
    key for key, entry in PROVIDER_CATALOG.items() if entry.visible
)


def normalize_provider_type(value: Any) -> str:
    # 参数保持 Any，输入是配置里的开放写法，可能是字符串或枚举，内部统一转字符串处理
    """归一化 provider 名，解析目录中声明的别名。"""
    if hasattr(value, "value"):
        value = value.value
    normalized = str(value or "").lower().strip()
    if normalized in PROVIDER_CATALOG:
        return normalized
    for key, entry in PROVIDER_CATALOG.items():
        if normalized in entry.aliases:
            return key
    return normalized


def provider_catalog_entry(value: Any) -> ProviderCatalogEntry:
    # 参数保持 Any，理由同上，归一化函数负责消化各种写法
    """按 provider 名返回目录项。

    未识别（拼错）的 provider 名会抛出 ValueError，不再静默回退到 ollama ——
    静默回退会把「配置写错」伪装成「莫名其妙跑在本地 ollama 上」。
    空值（None / ""）表示未设置，仍回退到 ollama 默认项。
    """
    normalized = normalize_provider_type(value)
    if normalized in PROVIDER_CATALOG:
        return PROVIDER_CATALOG[normalized]
    if not normalized:
        return PROVIDER_CATALOG["ollama"]
    raise ValueError(
        f"未知的模型 provider: {value!r}。"
        f"支持的 provider: {', '.join(sorted(PROVIDER_CATALOG))}"
    )


def provider_defaults(value: Any) -> dict[str, Any]:
    # 参数保持 Any，理由同上；返回值保持宽字典，各消费方取的键不一样
    """供 profile 与配置界面使用的扁平默认值视图。"""
    entry = provider_catalog_entry(value)
    return {
        "label": entry.label,
        "value": entry.value,
        "description": entry.description,
        "protocol": entry.protocol,
        "default_base_url": entry.resolved_default_base_url(),
        "runtime_default_base_url": entry.runtime_default_base_url(),
        "default_model_name": entry.default_model_name,
        "models": list(entry.models),
        "api_key_env": entry.api_key_env,
        "requires_api_key": entry.requires_api_key,
        "requires_base_url": entry.requires_base_url,
        "endpoint": entry.endpoint,
    }


def visible_provider_options() -> list[dict[str, Any]]:
    """返回用户可见的 provider 选项。"""
    return [provider_defaults(value) for value in USER_VISIBLE_PROVIDER_TYPES]


__all__ = [
    "PROVIDER_CATALOG",
    "USER_VISIBLE_PROVIDER_TYPES",
    "CompatSwitches",
    "ProviderCatalogEntry",
    "normalize_provider_type",
    "provider_catalog_entry",
    "provider_defaults",
    "visible_provider_options",
]
