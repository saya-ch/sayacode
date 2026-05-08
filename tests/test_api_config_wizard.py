from lib.api_config import APIConfig, APIConfigManager, APIType
from lib.api_config.wizard import APIConfigWizard, WizardConsole, _visible_api_types
from lib.i18n import get_language_preference, set_language
from lib.models.provider_catalog import USER_VISIBLE_PROVIDER_TYPES, provider_defaults


class FakeConsole:
    def __init__(self):
        self.items = []

    def print(self, text=""):
        self.items.append(text)


def test_wizard_console_print_accepts_empty_line():
    console = FakeConsole()
    wizard_console = WizardConsole(console)

    wizard_console.print()

    assert console.items == [""]


def test_wizard_accepts_provider_specific_api_key_formats(tmp_path):
    previous_language = get_language_preference()
    set_language("en")
    wizard = APIConfigWizard(manager=APIConfigManager(config_dir=str(tmp_path / "config")))

    try:
        ok, error = wizard._validate_api_key("letters-only-token", APIType.OPENAI)
    finally:
        set_language(previous_language)

    assert ok is True
    assert error == ""


def test_wizard_visible_protocols_follow_provider_catalog():
    assert tuple(api_type.value for api_type in _visible_api_types()) == USER_VISIBLE_PROVIDER_TYPES


def test_wizard_add_makes_new_profile_current(tmp_path, monkeypatch):
    manager = APIConfigManager(config_dir=str(tmp_path / "config"))
    assert manager.add_config(
        "old",
        APIConfig(
            api_type=APIType.OLLAMA,
            base_url="http://localhost:11434",
            model_name="old-model",
            context_window=4096,
        ),
    )
    wizard = APIConfigWizard(manager=manager)
    monkeypatch.setattr(wizard, "_select_api_type", lambda: APIType.OLLAMA)
    monkeypatch.setattr(wizard, "_input_base_url", lambda api_type: "http://localhost:11434")
    monkeypatch.setattr(wizard, "_input_api_key", lambda api_type, base_url=None: "")
    monkeypatch.setattr(wizard, "_input_model_name", lambda api_type: "new-model")
    monkeypatch.setattr(wizard, "_input_extra_config", lambda api_type: {})
    monkeypatch.setattr(
        wizard,
        "_resolve_context_window",
        lambda api_type, base_url, api_key, model_name, extra_config: 4096,
    )

    config = wizard.run("new")

    assert config is not None
    assert manager.current_config_name == "new"
    assert manager.get_current_config().model_name == "new-model"


def test_wizard_api_key_prompt_uses_visible_input_for_custom_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    wizard = APIConfigWizard(manager=APIConfigManager(config_dir=str(tmp_path / "config")))
    monkeypatch.setattr(wizard.console, "input", lambda prompt: "")
    monkeypatch.setattr(
        wizard.console,
        "secret_input",
        lambda prompt: (_ for _ in ()).throw(AssertionError("secret input should not be used")),
    )

    assert wizard._input_api_key(APIType.OPENAI, "http://localhost:8000/v1") == ""


def test_wizard_api_key_prompt_uses_visible_input_with_env_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "envkey")
    wizard = APIConfigWizard(manager=APIConfigManager(config_dir=str(tmp_path / "config")))
    monkeypatch.setattr(wizard.console, "input", lambda prompt: "")
    monkeypatch.setattr(
        wizard.console,
        "secret_input",
        lambda prompt: (_ for _ in ()).throw(AssertionError("secret input should not be used")),
    )

    assert wizard._input_api_key(APIType.OPENAI, "https://api.openai.com/v1") == ""


def test_api_config_allows_custom_openai_compatible_endpoint_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="http://localhost:8000/v1",
        model_name="local-model",
    )

    ok, error = config.validate()

    assert ok is True
    assert error == ""


def test_api_config_allows_loopback_http_endpoint(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="http://127.0.0.1:8000/v1",
        model_name="local-model",
    )

    ok, error = config.validate()

    assert ok is True
    assert error == ""


def test_api_config_rejects_localhost_prefix_spoof(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="http://localhost.evil.com/v1",
        model_name="local-model",
    )

    ok, error = config.validate()

    assert ok is False
    assert "HTTPS" in error


def test_api_config_rejects_url_without_host(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="https:///v1",
        model_name="local-model",
    )

    ok, error = config.validate()

    assert ok is False
    assert error


def test_api_config_accepts_uppercase_url_scheme(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "envkey")
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="HTTPS://api.openai.com/v1",
        model_name="gpt-test",
    )

    ok, error = config.validate()

    assert ok is True
    assert error == ""


def test_wizard_base_url_uses_exact_loopback_detection(tmp_path):
    wizard = APIConfigWizard(manager=APIConfigManager(config_dir=str(tmp_path / "config")))

    loopback_ok, loopback_error = wizard._validate_base_url("http://127.0.0.1:8000/v1", APIType.OPENAI)
    spoof_ok, spoof_error = wizard._validate_base_url("http://localhost.evil.com/v1", APIType.OPENAI)

    assert loopback_ok is True
    assert loopback_error == ""
    assert spoof_ok is False
    assert spoof_error


def test_api_config_uses_provider_default_model_when_missing():
    config = APIConfig(
        api_type=APIType.OLLAMA,
        base_url="http://localhost:11434",
    )

    assert config.model_name == provider_defaults("ollama")["default_model_name"]


def test_api_config_requires_key_for_default_hosted_endpoint(monkeypatch):
    previous_language = get_language_preference()
    set_language("en")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="https://api.openai.com/v1",
        model_name="gpt-test",
    )

    try:
        ok, error = config.validate()
    finally:
        set_language(previous_language)

    assert ok is False
    assert "requires an API key" in error


def test_api_config_manager_does_not_persist_environment_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "envkey")
    manager = APIConfigManager(config_dir=str(tmp_path / "config"))

    assert manager.add_config(
        "openai-env",
        APIConfig(
            api_type=APIType.OPENAI,
            base_url="https://api.openai.com/v1",
            model_name="gpt-test",
            api_key="envkey",
        ),
    )

    stored = (tmp_path / "config" / "api_configs.json").read_text(encoding="utf-8")

    assert "envkey" not in stored
    assert "OPENAI_API_KEY" in stored


def test_api_config_manager_persists_manually_entered_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "envkey")
    manager = APIConfigManager(config_dir=str(tmp_path / "config"))

    assert manager.add_config(
        "openai-manual",
        APIConfig(
            api_type=APIType.OPENAI,
            base_url="https://api.openai.com/v1",
            model_name="gpt-test",
            api_key="manual",
        ),
    )

    stored = (tmp_path / "config" / "api_configs.json").read_text(encoding="utf-8")

    assert "manual" in stored
