import pytest

from lib.api_config import APIType
from lib.models import AzureOpenAIModel, OllamaModel
from lib.models.provider_catalog import USER_VISIBLE_PROVIDER_TYPES, provider_defaults
from lib.models.registry import get_model_provider_registry
from lib.runtime.model_profiles import provider_defaults as runtime_provider_defaults


def test_provider_registry_creates_ollama_model():
    registry = get_model_provider_registry()
    model = registry.create_model("ollama", model_name="unit-test", context_window="256k")

    assert isinstance(model, OllamaModel)
    assert model.context_window == 262144


def test_provider_registry_resolves_azure_alias():
    registry = get_model_provider_registry()
    model = registry.create_model(
        "azure_openai",
        model_name="deployment",
        base_url="https://example.openai.azure.com",
        api_key="x",
        context_window="128k",
    )

    assert isinstance(model, AzureOpenAIModel)
    assert model.model_name == "deployment"
    assert model.context_window == 131072


def test_registry_lists_and_resolves_aliases():
    registry = get_model_provider_registry()

    assert "ollama" in registry.list_types()
    assert registry.get_model_class("azure_openai") is AzureOpenAIModel


def test_registry_validates_profile_and_detects_manual_context_window():
    registry = get_model_provider_registry()
    valid, error = registry.validate_profile(
        "ollama",
        "unit-test",
        context_window="256k",
    )

    assert valid is True
    assert error == ""
    assert registry.detect_context_window(
        "ollama",
        "unit-test",
        context_window="256k",
    ) == 262144


def test_registry_create_from_config_uses_provider_default_model():
    registry = get_model_provider_registry()
    model = registry.create_from_config({"api_type": "ollama", "context_window": 4096})

    assert isinstance(model, OllamaModel)
    assert model.model_name == provider_defaults("ollama")["default_model_name"]


def test_provider_catalog_drives_api_type_and_runtime_defaults():
    for api_type in APIType:
        defaults = provider_defaults(api_type.value)

        assert api_type.default_base_url == defaults["default_base_url"]
        assert api_type.default_model == defaults["default_model_name"]
        assert api_type.endpoint == defaults["endpoint"]
        assert api_type.requires_api_key == defaults["requires_api_key"]
        assert api_type.api_key_env == defaults["api_key_env"]
        assert runtime_provider_defaults(api_type.value) == defaults


def test_registry_specs_use_provider_catalog_defaults():
    registry = get_model_provider_registry()

    for api_type in APIType:
        defaults = provider_defaults(api_type.value)
        spec = registry.get(api_type.value)

        assert spec.default_base_url == defaults["runtime_default_base_url"]
        assert spec.default_model_name == defaults["default_model_name"]
        assert spec.requires_api_key == defaults["requires_api_key"]
        assert spec.env_var == defaults["api_key_env"]


def test_cli_visible_model_types_follow_provider_catalog():
    from lib import cli

    assert tuple(cli.USER_VISIBLE_MODEL_TYPES) == USER_VISIBLE_PROVIDER_TYPES
    assert list(cli.PROTOCOL_DEFAULTS) == list(USER_VISIBLE_PROVIDER_TYPES)


def test_cli_protocol_defaults_are_read_from_catalog_at_call_time(monkeypatch):
    from lib import cli

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:15555")

    assert cli._get_protocol_option("ollama")["default_base_url"] == "http://localhost:15555"


# ── CLI 协议解析必须与目录一致 ────────────────────────────────────────────────
#
# 「可见」（出现在选择菜单里）与「可解析」是两件事。把它们混为一谈的后果不只是
# 显示错误：configure_model 会把解析结果写回用户配置。


@pytest.mark.parametrize("value", ["generic", "azure_openai"])
def test_protocol_option_resolves_providers_that_are_not_in_the_menu(value):
    """不可见 provider 也必须解析成自己，而不是静默变成 Ollama。

    回归保护：早期实现只在 ``USER_VISIBLE_PROVIDER_TYPES`` 里查表，查不到就返回
    ollama。于是已保存模型卡片会把自定义端点显示成「协议 Ollama」，更糟的是
    ``configure_model`` 会把回退值（``api_type=ollama`` / ``qwen3.5:9b`` /
    ``http://localhost:11434``）写回用户配置 —— 「配置写错」被伪装成
    「莫名其妙跑在本地 ollama 上」。
    """
    from lib.cli.configure import _get_protocol_option

    option = _get_protocol_option(value)

    assert option["value"] == value
    assert option["label"] != "Ollama"
    assert "localhost" not in option["default_base_url"]


def test_protocol_option_normalizes_aliases():
    """别名（``azure``）也要解析成目录里的正式键。"""
    from lib.cli.configure import _get_protocol_option

    assert _get_protocol_option("azure")["value"] == "azure_openai"


def test_protocol_option_rejects_a_typo_instead_of_becoming_ollama():
    """拼错的 provider 必须报错 —— 与 ``provider_catalog_entry`` 的既有约定一致。"""
    from lib.cli.configure import _get_protocol_option

    with pytest.raises(ValueError):
        _get_protocol_option("ollamma")


@pytest.mark.parametrize("unset", [None, "", "   "])
def test_protocol_option_defaults_to_ollama_only_when_unset(unset):
    """「还没选」仍回退 ollama —— 这是合理默认，与「拼错」不是一回事。"""
    from lib.cli.configure import _get_protocol_option

    assert _get_protocol_option(unset)["value"] == "ollama"


# ── model / model_name 别名不得撞成重复关键字 ────────────────────────────────


def test_create_model_accepts_both_model_and_model_name():
    """两个名字同时给出时，显式参数优先，且不得抛重复关键字。

    回归保护：早期实现只在不传 ``model_name`` 时才 ``pop("model")``。两个都给时
    ``model`` 留在 ``init_kwargs`` 里，与显式传入的 ``model=`` 撞成
    ``TypeError: got multiple values for keyword argument 'model'``。
    """
    registry = get_model_provider_registry()

    model = registry.create_model(
        "openai", model_name="explicit", model="from-kwargs", api_key="dummy"
    )

    assert model.model_name == "explicit"
    assert "model" not in (getattr(model, "model_kwargs", None) or {})


def test_create_model_falls_back_to_the_model_alias():
    """只给 ``model`` 时它必须生效 —— 别名回退不能被顺手删掉。"""
    registry = get_model_provider_registry()

    model = registry.create_model("openai", model="from-kwargs", api_key="dummy")

    assert model.model_name == "from-kwargs"


def test_create_model_without_any_name_uses_the_catalog_default():
    registry = get_model_provider_registry()

    model = registry.create_model("openai", api_key="dummy")

    assert model.model_name == provider_defaults("openai")["default_model_name"]


def test_create_model_keeps_context_window_out_of_model_kwargs():
    """工厂路径同样不得把 ``context_window`` 变成请求体参数。"""
    registry = get_model_provider_registry()

    model = registry.create_model("openai", model_name="m", api_key="dummy",
                                  context_window="256k")

    assert model.context_window == 262144
    assert "context_window" not in (getattr(model, "model_kwargs", None) or {})
