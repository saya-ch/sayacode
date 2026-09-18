# model_profiles 缺口：归一化、凭证、转换、切换分支。

import pytest

from lib.api_config import APIConfig, APIConfigManager, APIType
from lib.runtime import model_profiles as mp


def _mgr(tmp_path):
    # 隔离 profile 仓库固件。
    return APIConfigManager(config_dir=str(tmp_path / "cfg"))


def _cfg(**kw):
    kw.setdefault("api_type", APIType.OLLAMA)
    kw.setdefault("base_url", "http://localhost:11434")
    kw.setdefault("model_name", "m")
    return APIConfig(**kw)


class TestNormalize:
    def test_variants(self):
        assert mp.normalize_api_type(APIType.DEEPSEEK) == "deepseek"

        class Valued:
            value = " Ollama "

        assert mp.normalize_api_type(Valued()) == "ollama"
        assert mp.normalize_api_type("DeepSeek") == "deepseek"
        assert mp.normalize_api_type(None) == ""
        assert mp.provider_defaults(None)["value"] == "ollama"

    def test_requires_completion(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        assert mp.profile_requires_completion(_cfg()) is False
        deep = _cfg(api_type=APIType.DEEPSEEK, base_url="https://api.deepseek.com/v1")
        assert mp.profile_requires_completion(deep) is True
        assert mp.profile_requires_completion(deep, env_getter=lambda name: None) is True
        assert mp.profile_requires_completion(_cfg(api_type=APIType.DEEPSEEK, base_url="https://api.deepseek.com/v1",
                                                   api_key="k")) is False

    def test_sanitize(self):
        assert mp.sanitize_base_url(None) is None
        assert mp.sanitize_base_url("https://x.ai") == "https://x.ai"
        assert mp.sanitize_base_url("ftp://x") is None

    def test_extract_metadata(self):
        assert mp.extract_context_window_from_config({"metadata": {"max_context_len": 9000}}) == 9000
        assert mp.extract_context_window_from_config({"metadata": "oops"}) is None
        assert mp.extract_context_window_from_config({}) is None

    def test_store_invalid(self):
        with pytest.raises(ValueError):
            mp.store_context_window_in_config({}, "oops")


class TestConvert:
    def test_full_fields(self):
        cfg = _cfg(api_key="k", temperature=0.5, max_tokens=100, context_window=8000,
                   timeout=30, max_retries=5, azure_api_version="v", azure_deployment="d",
                   metadata={"k": "v"})
        out = mp.runtime_model_config_from_profile(cfg)
        assert out["api_key"] == "k" and out["temperature"] == 0.5
        assert out["max_tokens"] == 100 and out["context_window"] == 8000
        assert out["azure_deployment"] == "d" and out["metadata"] == {"k": "v"}

    def test_env_key_stripped(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
        back = mp.runtime_to_api_config("deepseek", "m", {"api_key": "env-key", "base_url": "notaurl"})
        assert back.api_key == ""
        assert "api.deepseek" in back.base_url

    def test_build_name_collision(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.add_config("ollama-m", _cfg())
        assert mp.build_profile_name(mgr, "ollama", "m") == "ollama-m-2"
        mgr.add_config("ollama-m-2", _cfg())
        assert mp.build_profile_name(mgr, "ollama", "m") == "ollama-m-3"

    def test_save_paths(self, tmp_path, monkeypatch):
        mgr = _mgr(tmp_path)
        assert mp.save_model_profile(mgr, "ollama", "m", {}) is not None
        monkeypatch.setattr(mgr, "add_config", lambda *a, **k: False)
        assert mp.save_model_profile(mgr, "ollama", "m", {}) is None

    def test_current_fallback(self, tmp_path):
        mgr = _mgr(tmp_path)
        assert mp.get_current_saved_profile(mgr) == (None, None)
        mgr.add_config("p1", _cfg())
        mgr.current_config_name = None
        name, cfg = mp.get_current_saved_profile(mgr)
        assert name == "p1" and cfg is not None


class TestSwitch:
    def _state(self, tmp_path):
        from lib.state import create_app_state

        return create_app_state(tmp_path)

    def test_no_profile(self, tmp_path):
        out = mp.switch_active_profile(object(), self._state(tmp_path), api_manager=_mgr(tmp_path))
        assert (out.ok, out.error) == (False, "no_saved_profile")

    def test_ensure_raises(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.add_config("p1", _cfg())
        mgr.set_current("p1")

        def boom(*a, **k):
            raise RuntimeError("nope")

        out = mp.switch_active_profile(object(), self._state(tmp_path), api_manager=mgr, ensure_context_window=boom)
        assert out.ok is False and out.profile_name == "p1"

    def test_ensure_adds_window(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.add_config("p1", _cfg())
        mgr.set_current("p1")
        agent = SimpleNamespaceShim()
        state = self._state(tmp_path)
        out = mp.switch_active_profile(agent, state, api_manager=mgr,
                                       ensure_context_window=lambda *a, **k: a[2].update(context_window=8000))
        assert out.ok is True

    def test_no_change(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.add_config("p1", _cfg())
        mgr.set_current("p1")
        agent = SimpleNamespaceShim()
        state = self._state(tmp_path)
        first = mp.switch_active_profile(agent, state, api_manager=mgr)
        assert first.ok is True and first.changed is True
        second = mp.switch_active_profile(agent, state, api_manager=mgr)
        assert second.ok is True and second.changed is False

    def test_create_fail(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.add_config("p1", _cfg())
        mgr.set_current("p1")
        agent = SimpleNamespaceShim()
        state = self._state(tmp_path)
        import lib.runtime.model_profiles as _mp

        real = _mp.create_runtime_model
        _mp.create_runtime_model = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            out = mp.switch_active_profile(agent, state, api_manager=mgr)
        finally:
            _mp.create_runtime_model = real
        assert out.ok is False


class SimpleNamespaceShim:
    # switch 用的最小 agent 替身。
    def __init__(self):
        self.model = None
        self.session = None
        self.tools = []

    def _create_agent(self):
        pass
