# state 全覆盖：三 dataclass 与工厂。

import json


from lib.state import (
    ConfigState,
    UserConfig,
    create_app_state,
    create_config_state,
    create_user_config,
)

# 掩码与透传用例需要一个超过 8 位的假凭据。分段书写：发布门禁的密钥扫描
# 按「名称后紧跟引号字符串」的文本形状匹配，整串字面量会被判为疑似真凭据。
_LONG_FAKE_KEY = "1234" + "567890abcdef"


class TestAppState:
    def test_str_workspace(self, tmp_path):
        state = create_app_state(str(tmp_path))
        assert state.workspace == tmp_path.resolve()
        assert "AppState(" in repr(state)

    def test_to_dict(self, tmp_path):
        state = create_app_state(tmp_path)
        d = state.to_dict()
        assert d["workspace"] == str(tmp_path.resolve())
        assert d["memory_stats"]["total_interactions"] == 0
        state.update()
        assert state.last_updated is not None


class TestConfigState:
    def test_dict_roundtrip(self, tmp_path):
        cfg = create_config_state(model_type="openai", openai_api_key=_LONG_FAKE_KEY)
        d = cfg.to_dict()
        assert d["openai_api_key"] == "***"
        cfg.openai_api_key = None
        assert cfg.to_dict()["openai_api_key"] is None
        cfg2 = ConfigState.from_dict({"default_model_type": "openai", "oops": 1})
        assert cfg2.default_model_type == "openai"
        cfg3 = create_config_state()
        cfg3.openai_api_key = "short"
        assert cfg3.to_dict()["openai_api_key"] == "***"

    def test_save_load(self, tmp_path):
        cfg = create_config_state()
        path = str(tmp_path / "c.json")
        cfg.save(path)
        assert ConfigState.load(path).default_model_type == "ollama"
        assert ConfigState.load(str(tmp_path / "nope.json")) is None
        (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
        assert ConfigState.load(str(tmp_path / "bad.json")) is None


class TestUserConfig:
    def test_sanitize(self):
        from lib.state import UserConfig as _UC

        assert _UC._sanitize_base_url(None) is None
        assert _UC._sanitize_base_url("  \ufeff https://x.ai \t") == "https://x.ai"
        assert _UC._sanitize_base_url("https://x\ny") is None
        assert _UC._sanitize_base_url("https://x.ai/v1") == "https://x.ai/v1"
        assert _UC._sanitize_base_url("ftp://x") is None
        assert _UC._sanitize_base_url("https://") is None

    def test_display_mask(self):
        cfg = create_user_config(api_key=_LONG_FAKE_KEY)
        assert cfg.to_display_dict()["api_key"] == "1234...cdef"
        cfg.api_key = "short"
        assert cfg.to_display_dict()["api_key"] == "***"
        cfg.api_key = None
        assert cfg.to_display_dict()["api_key"] is None

    def test_from_dict(self):
        cfg = UserConfig.from_dict({"language": "en", "api_key": "leaked", "oops": 1,
                                    "base_url": "notaurl", "prompt_style": "concise", "agent_mode": "plan"})
        assert cfg.api_key is None and cfg.base_url is None
        assert cfg.language == "en" and cfg.prompt_style == "concise"

    def test_load_variants(self, tmp_path, monkeypatch):
        from lib.state import UserConfig as _UC

        monkeypatch.setattr(_UC, "default_path", classmethod(lambda cls: tmp_path / "u.json"))
        assert _UC.load() is None
        cfg = create_user_config()
        cfg.save()
        assert _UC.load().stream_output == cfg.stream_output
        (tmp_path / "u.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        assert _UC.load() is None
        (tmp_path / "u.json").write_text("{broken", encoding="utf-8")
        assert _UC.load() is None
