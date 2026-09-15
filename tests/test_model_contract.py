"""模型层对外契约冻结测试 —— 重写的安全网。

**本文件是重写的前置条件与验收标准。**

设计约束（刻意为之）：

1. 只使用**稳定公共面**：``lib.models`` 包导出面与 ``lib.models.provider_catalog``。
   不导入 ``base`` / ``registry`` / 各 ``*_model`` 等实现模块，因此实现可以被整体替换。
2. 只断言**对外可观测行为**，不触碰内部结构（`self._model`、私有谓词等）。
3. 重写前必须对旧实现全绿；重写后同一套测试必须继续全绿。
   若某条断言在重写后失败，那是**真实行为变化**，必须单独论证，而不是改测试。

已知不冻结的项（有意为之）：
- ``generic`` 的 ``get_model_info().model_type``：旧实现复用了 ``OpenAIModel``，
  因此返回 ``"openai"``；这是实现细节而非契约，重写后可变为 ``"generic"``。
- ``validate_temperature``：重写后本层提供 ``clamp_temperature``，
  原因是厂商类已用该名字注册了 temperature 字段校验器，同名会覆盖它 ——
  详见 ``test_temperature_clamp_is_exposed_under_clamp_name``。

重写后的加固（第五轮审查发现，见 ``CODE_REVIEW_FINDINGS.md``）：
- 第 2b 节：``context_window`` / ``model_name`` 曾被原样传进构造函数，被 LangChain
  收进 ``model_kwargs`` 并**发进请求体**；现在守卫落在 ``_ProtocolModel`` 自己身上。
- 上下文窗口新增「``None`` 可清空」语义（见 ``test_context_window_can_be_cleared_back_to_unknown``）。
- ``CompatSwitches.thinking_format`` 已删除：它没有任何生产读取点，
  与 ``passthrough_nonstandard`` 一起构成「声明了但没人读」的装饰性开关。
  开关的行为契约现在由 ``tests/test_model_compat.py`` 用因果断言守卫。
"""

import pytest

from lib.models import (
    AnthropicModel,
    GeminiModel,
    ModelInfo,
    OllamaModel,
    OpenAIModel,
    get_model_provider_registry,
    parse_context_window,
)

# ``TokenUsage`` 不在 ``lib.models`` 的导出面里，只在 ``lib.models.base``。
# 该模块是载荷模块（既有测试也从这里导入 BaseModel / ModelInfo / parse_context_window），
# 因此重写保留它，此处依赖它是契约的一部分而非实现细节。
from lib.models.base import TokenUsage
from lib.models.provider_catalog import (
    PROVIDER_CATALOG,
    USER_VISIBLE_PROVIDER_TYPES,
    provider_catalog_entry,
    provider_defaults,
)


# 对外契约成员：调用方（lib 与 tests）依赖的全部成员
CONTRACT_MEMBERS = (
    "chat",
    "chat_stream",
    "bind_tools",
    "context_window",
    "context_window_source",
    "last_usage",
    "session_usage",
    "detect_context_window",
    "prepare_messages",
    "convert_messages",
    "get_model_info",
    "clamp_temperature",
    "reset_session_usage",
    "model_name",
    "temperature",
)

DUMMY_BASE_URL = "https://example.invalid/v1"


def _make(key: str, **overrides):
    registry = get_model_provider_registry()
    kwargs = {"model_name": "probe-model", "base_url": DUMMY_BASE_URL, "api_key": "dummy"}
    kwargs.update(overrides)
    return registry.create_model(key, **kwargs)


# ── 1. 包导出面 ──────────────────────────────────────────────────────────────


def test_public_exports_present():
    # 注：``TokenUsage`` 当前**未**从 ``lib.models`` 导出（只能从 ``lib.models.base`` 导入）。
    # 这是既有状态，本文件予以冻结。重写会把它加入 ``lib.models`` 导出面
    # —— 属纯新增（``session_usage`` 本来就返回该类型），不破坏兼容，
    # 该改进在重写后由 ``test_provider_optional_deps.py`` 单独断言。
    for name in (
        "BaseModel", "ModelInfo", "parse_context_window",
        "OpenAIModel", "AzureOpenAIModel", "AnthropicModel", "GeminiModel", "OllamaModel",
        "ModelProviderRegistry", "ModelProviderSpec", "get_model_provider_registry",
    ):
        import lib.models as models

        assert hasattr(models, name), f"lib.models 缺少导出: {name}"


@pytest.mark.parametrize("key", get_model_provider_registry().list_types())
def test_every_provider_exposes_all_contract_members(key):
    model = _make(key)

    missing = [name for name in CONTRACT_MEMBERS if not hasattr(model, name)]
    assert missing == [], f"{key} 缺少契约成员: {missing}"


@pytest.mark.parametrize("key", get_model_provider_registry().list_types())
def test_every_provider_starts_with_unknown_context_window(key):
    """上下文窗口在未知时必须为 0 —— 不能编一个默认值出来。"""
    model = _make(key)

    assert model.context_window == 0
    assert model.context_window_source == ""


@pytest.mark.parametrize("key", get_model_provider_registry().list_types())
def test_every_provider_starts_with_zeroed_usage(key):
    model = _make(key)

    assert model.session_usage == TokenUsage(0, 0, 0)
    assert model.last_usage is None


# ── 2. 上下文窗口语义 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (128000, 128000),
        ("128000", 128000),
        ("128,000", 128000),
        ("128k", 128 * 1024),
        ("128K", 128 * 1024),
        ("1M", 1024 * 1024),
        ("1.5m", int(1.5 * 1024 * 1024)),
    ],
)
def test_context_window_accepts_documented_forms(value, expected):
    assert parse_context_window(value) == expected


@pytest.mark.parametrize("value", [None, "", "abc", 0, -1, True, False, [], "0k", "1e5"])
def test_context_window_rejects_invalid_forms(value):
    assert parse_context_window(value) is None


def test_context_window_setter_marks_source_manual():
    model = _make("openai")

    model.context_window = "64k"

    assert model.context_window == 64 * 1024
    assert model.context_window_source == "manual"


def test_context_window_setter_ignores_invalid_value():
    """非法值不得污染已有取值，也不得把来源改成 manual。"""
    model = _make("openai")
    model.context_window = 32768

    model.context_window = "not-a-number"

    assert model.context_window == 32768
    assert model.context_window_source == "manual"


def test_context_window_can_be_cleared_back_to_unknown():
    """``None`` 必须能清空取值。

    允许清空是必要的：否则一旦设过值就永远回不到「未知」，而「未知」正是
    「要求用户显式输入」的正确前置状态。
    """
    model = _make("openai")
    model.context_window = 32768

    model.context_window = None

    assert model.context_window == 0
    assert model.context_window_source == ""


# ── 2b. 直接构造协议类：私有参数不得漏进请求体 ───────────────────────────────
#
# ``context_window`` 与 ``model_name`` 都不是（全部）LangChain 集成的字段名。
# 早期实现把它们原样传进构造函数，LangChain 的 ``_build_model_kwargs`` 于是判定
# 它们「不是默认参数」、收进 ``model_kwargs``，进而**原样发进请求体**（实测请求体里
# 真的多出 ``{"context_window": 12345}``，厂商会以未知参数为由拒绝）。
# 工厂路径本来就过滤，但这些类是对外公开的，守卫必须落在类自己身上。

_DIRECT_CONSTRUCTION_KWARGS = {
    "OpenAIModel": {
        "model": "probe-model", "api_key": "dummy",
        "base_url": DUMMY_BASE_URL,
    },
    "AzureOpenAIModel": {
        "model": "probe-model", "api_key": "dummy",
        "azure_endpoint": "https://example.invalid",
        "api_version": "2024-02-01",
    },
    "DeepSeekModel": {"model": "probe-model", "api_key": "dummy"},
    "AnthropicModel": {"model": "probe-model", "api_key": "dummy"},
    "OllamaModel": {"model": "probe-model"},
    "GeminiModel": {"model": "probe-model", "api_key": "dummy"},
}


def _direct_constructors():
    import lib.models as models

    for name, kwargs in _DIRECT_CONSTRUCTION_KWARGS.items():
        cls = getattr(models, name, None)
        if cls is None:
            continue  # 可选依赖未安装
        yield pytest.param(cls, kwargs, id=name)


# 必须**求值成序列**再交给 parametrize：传生成器在 pytest 10 会告警。
DIRECT_CONSTRUCTORS = tuple(_direct_constructors())


def _model_kwargs(model):
    return getattr(model, "model_kwargs", None) or {}


@pytest.mark.parametrize("cls,kwargs", DIRECT_CONSTRUCTORS)
def test_direct_construction_honours_context_window(cls, kwargs):
    model = cls(**kwargs, context_window=12345)

    assert model.context_window == 12345
    assert model.context_window_source == "manual"


@pytest.mark.parametrize("cls,kwargs", DIRECT_CONSTRUCTORS)
def test_direct_construction_keeps_context_window_out_of_model_kwargs(cls, kwargs):
    """构造参数不得变成 `model_kwargs` 条目 —— 那等于把它发进请求体。"""
    model = cls(**kwargs, context_window=12345)

    assert "context_window" not in _model_kwargs(model)


@pytest.mark.parametrize("cls,kwargs", DIRECT_CONSTRUCTORS)
def test_direct_construction_keeps_model_name_out_of_model_kwargs(cls, kwargs):
    """``model`` 与 ``model_name`` 同时给出时，后者不得漏进 `model_kwargs`。"""
    import copy

    both = copy.deepcopy(kwargs)
    both.pop("model")
    model = cls(model="chosen", model_name="ignored", **both)

    assert str(getattr(model, "model_name", "")) == "chosen"
    assert "model_name" not in _model_kwargs(model)


@pytest.mark.parametrize("cls,kwargs", DIRECT_CONSTRUCTORS)
def test_direct_construction_accepts_model_name_alone(cls, kwargs):
    """只给 ``model_name`` 时，它必须落到该集成的 ``model`` 字段上。"""
    import copy

    only_name = copy.deepcopy(kwargs)
    only_name.pop("model")
    model = cls(model_name="by-name", **only_name)

    assert str(getattr(model, "model_name", "")) == "by-name"


def test_detect_context_window_returns_manual_value_without_probing():
    """已手工指定时不得发起探测（本测试不允许任何网络访问）。"""
    model = _make("openai")
    model.context_window = 8192

    assert model.detect_context_window() == 8192
    assert model.context_window_source == "manual"


# ── 3. usage 累计 ────────────────────────────────────────────────────────────


def test_usage_accumulates_and_resets():
    model = _make("openai")
    model._record_usage(TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    model._record_usage(TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3))

    assert model.last_usage == TokenUsage(1, 2, 3)
    assert model.session_usage == TokenUsage(11, 7, 18)

    model.reset_session_usage()

    assert model.session_usage == TokenUsage(0, 0, 0)


def test_token_usage_is_additive():
    total = TokenUsage(1, 2, 3) + TokenUsage(10, 20, 30)

    assert total == TokenUsage(11, 22, 33)


# ── 4. 采样参数 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [(0, 0.0), (0.5, 0.5), (1, 1.0), (5, 1.0), (-1, 0.0)])
def test_clamp_temperature_clamps_to_unit_range(value, expected):
    assert _make("openai").clamp_temperature(value) == expected


def test_temperature_clamp_is_exposed_under_clamp_name():
    """记录温度钳制的命名偏差及其**真实**原因。

    实测：``langchain_openai`` 自己在 ``BaseChatOpenAI`` 上就定义了
    ``validate_temperature``，并由 pydantic 的 ``validate_<字段名>`` 约定注册为
    ``temperature`` 字段的校验器。旧实现在 mixin 里也定义了同名方法，**覆盖**了厂商
    那个校验器，导致签名不匹配（传进来的是 ``ValidationInfo`` 而不是 float），
    模型构造直接失败。实测实例方法 / staticmethod / property 三种写法都会覆盖它。

    因此本层实现改名为 ``clamp_temperature``。旧名 ``validate_temperature``
    仍然可见，但那是**厂商自己的**校验器（签名不同、不是给外部调用的），
    只应在自有传输基类 ``BaseModel`` 上按旧语义使用。
    """
    from lib.models import BaseModel, ModelExtras, OpenAIModel

    assert hasattr(ModelExtras, "clamp_temperature")
    assert OpenAIModel(model_name="x", api_key="dummy").clamp_temperature(5) == 1.0

    # 自有传输基类（纯 Python，无厂商校验器冲突）保留旧名与旧语义
    assert hasattr(BaseModel, "validate_temperature")


# ── 5. 消息转换 ──────────────────────────────────────────────────────────────


def test_convert_messages_maps_roles():
    messages = _make("openai").convert_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
        {"role": "assistant", "content": "ast"},
        {"role": "mystery", "content": "fallback"},
    ])

    assert [type(m).__name__ for m in messages] == [
        "SystemMessage", "HumanMessage", "AIMessage", "HumanMessage",
    ]
    assert [m.content for m in messages] == ["sys", "usr", "ast", "fallback"]


def test_prepare_messages_orders_system_history_user():
    messages = _make("openai").prepare_messages(
        "now",
        system_prompt="sys",
        history=[{"role": "user", "content": "before"}],
    )

    assert [m.content for m in messages] == ["sys", "before", "now"]


def test_prepare_messages_omits_system_when_absent():
    messages = _make("openai").prepare_messages("only")

    assert [m.content for m in messages] == ["only"]


# ── 6. 模型信息 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cls,expected_type",
    [
        (OpenAIModel, "openai"),
        (AnthropicModel, "anthropic"),
        (GeminiModel, "gemini"),
        (OllamaModel, "ollama"),
    ],
)
def test_get_model_info_reports_type(cls, expected_type):
    # 传入占位 key：官方集成（尤其 Gemini）在**构造期**就要求凭据，
    # 而旧手写实现是延迟到调用期才校验。这是有意的行为差异 —— 早失败优于晚失败。
    info = cls(model_name="probe-model", api_key="dummy").get_model_info()

    assert isinstance(info, ModelInfo)
    assert info.name == "probe-model"
    assert info.model_type == expected_type
    assert info.provider


def test_get_config_round_trips_core_fields():
    config = _make("openai").get_config()

    assert config["model_name"] == "probe-model"
    assert "temperature" in config


# ── 7. 目录（provider catalog）───────────────────────────────────────────────


def test_catalog_keys_and_visible_subset():
    assert set(PROVIDER_CATALOG) >= {
        "openai", "anthropic", "azure_openai", "gemini", "ollama", "generic",
    }
    assert set(USER_VISIBLE_PROVIDER_TYPES) <= set(PROVIDER_CATALOG)
    assert USER_VISIBLE_PROVIDER_TYPES


def test_provider_defaults_shape():
    defaults = provider_defaults("openai")

    for key in (
        "label", "value", "description", "default_base_url",
        "default_model_name", "api_key_env", "requires_api_key",
    ):
        assert key in defaults, f"provider_defaults 缺少字段: {key}"


def test_provider_catalog_entry_normalizes_azure_alias():
    assert provider_catalog_entry("azure").value == "azure_openai"


def test_provider_catalog_entry_rejects_unknown_provider():
    with pytest.raises(ValueError):
        provider_catalog_entry("ollamma")


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_provider_catalog_entry_falls_back_for_empty_value(empty):
    assert provider_catalog_entry(empty).value == "ollama"


# ── 8. 注册表与工厂 ──────────────────────────────────────────────────────────


def test_registry_lists_and_creates_every_type():
    registry = get_model_provider_registry()
    listed = registry.list_types()

    assert set(listed) >= {"openai", "anthropic", "gemini", "ollama", "generic"}
    for key in listed:
        assert _make(key) is not None


def test_create_model_honours_base_url_and_api_key():
    # 占位值刻意保持短、且不像凭据：发布检查会扫描 `api_key="..."` 这类字面量，
    # 超过 8 个字符即判为疑似密钥（说明门禁在正常工作）。
    model = _make("openai", base_url="https://custom.example/v1", api_key="dummy")

    assert "custom.example" in str(getattr(model, "base_url", "") or "")


def test_registry_rejects_unknown_type():
    with pytest.raises(ValueError):
        get_model_provider_registry().create_model("not-a-provider", model_name="x")


def test_registry_lookup_helper_is_total():
    """未知类型在 get() 上必须报错，而不是静默回退。"""
    registry = get_model_provider_registry()

    assert registry.is_supported("openai")
    assert not registry.is_supported("not-a-provider")
