from lib.api_config import APIType
from lib.models.provider_catalog import USER_VISIBLE_PROVIDER_TYPES, provider_defaults
from lib.models.openai_model import AzureOpenAIModel
from lib.models.ollama_model import OllamaModel
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
