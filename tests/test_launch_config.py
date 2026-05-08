from lib.api_config import APIConfig, APIConfigManager, APIType
from lib.runtime.launch_config import LaunchModelOverrides, ModelLaunchResolver
from lib.runtime.model_profiles import profile_requires_completion, store_context_window_in_config


def _manager(tmp_path):
    return APIConfigManager(config_dir=str(tmp_path / "config"))


def test_launch_resolver_uses_saved_profile_and_persists_detected_context(tmp_path):
    manager = _manager(tmp_path)
    assert manager.add_config(
        "local",
        APIConfig(
            api_type=APIType.OLLAMA,
            base_url="http://localhost:11434",
            model_name="qwen-test",
        ),
    )
    manager.set_current("local")
    summaries = []

    def configure_model(**kwargs):
        raise AssertionError("saved profiles should not prompt for a new model")

    def ensure_context_window(model_type, model_name, model_config):
        return store_context_window_in_config(model_config, "64k")

    resolver = ModelLaunchResolver(
        api_manager=manager,
        configure_model=configure_model,
        ensure_context_window=ensure_context_window,
        interactive_input=False,
        on_saved_profile_summary=lambda *args: summaries.append(args),
    )

    result = resolver.resolve()

    assert result.active_profile == "local"
    assert result.model_type == "ollama"
    assert result.model_name == "qwen-test"
    assert result.model_config["context_window"] == 65536
    assert manager.get_config("local").context_window == 65536
    assert summaries and summaries[0][0] == "local"


def test_launch_resolver_cli_model_override_does_not_reuse_saved_context_window(tmp_path):
    manager = _manager(tmp_path)
    assert manager.add_config(
        "local",
        APIConfig(
            api_type=APIType.OLLAMA,
            base_url="http://localhost:11434",
            model_name="saved-model",
            context_window="32k",
        ),
    )
    captured = {}

    def configure_model(**kwargs):
        captured.update(kwargs)
        return "ollama", "override-model", {"base_url": kwargs["default_base_url"], "context_window": 32768}

    resolver = ModelLaunchResolver(
        api_manager=manager,
        configure_model=configure_model,
        ensure_context_window=lambda *_args: 0,
        interactive_input=False,
    )

    result = resolver.resolve(LaunchModelOverrides(model_name="override-model"))

    assert result.active_profile is None
    assert captured["default_model_type"] == "ollama"
    assert captured["default_model_name"] == "override-model"
    assert captured["default_base_url"] == "http://localhost:11434"
    assert captured["default_context_window"] is None


def test_launch_resolver_completes_missing_credentials_when_interactive(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manager = _manager(tmp_path)
    assert manager.add_config(
        "cloud",
        APIConfig(
            api_type=APIType.OPENAI,
            base_url="https://api.openai.com/v1",
            model_name="gpt-test",
            api_key="tmp",
            context_window=4096,
        ),
    )
    config = manager.get_config("cloud")
    config.api_key = ""
    manager._save_configs()
    warnings = []

    def configure_model(**kwargs):
        return "openai", "gpt-test", {
            "base_url": kwargs["default_base_url"],
            "api_key": "new",
            "context_window": 4096,
        }

    resolver = ModelLaunchResolver(
        api_manager=manager,
        configure_model=configure_model,
        ensure_context_window=lambda *_args: 4096,
        interactive_input=True,
        on_profile_missing_credentials=warnings.append,
    )

    result = resolver.resolve()

    assert result.active_profile == "cloud"
    assert result.model_config["api_key"] == "new"
    assert warnings == ["cloud"]
    assert manager.get_config("cloud").api_key == "new"


def test_profile_requires_completion_respects_provider_env(monkeypatch):
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="https://api.openai.com/v1",
        model_name="gpt-test",
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert profile_requires_completion(config) is True

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert profile_requires_completion(config) is False
