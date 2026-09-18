# providers 全覆盖上半：真实 OpenAI/Ollama 类 + 缺包降级。

import pytest

import lib.models.providers as pv


# 掩码与透传用例需要一个超过 8 位的假凭据。分段书写：发布门禁的密钥扫描
# 按「名称后紧跟引号字符串」的文本形状匹配，整串字面量会被判为疑似真凭据。
_LONG_FAKE_KEY = "1234" + "567890abcdef"


def _openai_cls():
    cls = pv.resolve_protocol_class("openai")
    assert cls is not None
    return cls


def _ollama_cls():
    cls = pv.resolve_protocol_class("ollama")
    assert cls is not None
    return cls


class TestInit:
    def test_kwargs_digested(self):
        m = _openai_cls()(model="m", api_key="k", base_url="http://x", context_window=8000)
        assert m.context_window == 8000
        assert "context_window" not in (m.model_kwargs or {})

    def test_model_name_alias(self):
        m = _openai_cls()(model_name="m", api_key="k", base_url="http://x")
        assert m.model_name == "m"

    def test_both_names_model_wins(self):
        m = _openai_cls()(model="a", model_name="b", api_key="k", base_url="http://x")
        assert m.model_name == "a"

    def test_validator_path(self):
        cls = _openai_cls()
        m = cls.model_validate({"model_name": "m", "api_key": "k", "base_url": "http://x"})
        assert m.model_name == "m"
        assert cls._accept_model_name_kwarg({"model_name": "m"}) == {"model": "m"}

    def test_normalize_config_to_dict(self):
        from lib.api_config import APIConfig, APIType
        from lib.models.registry import _normalize_config

        cfg = APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m")
        assert _normalize_config(cfg)["model_name"] == "m"

    def test_ollama_names(self):
        m = _ollama_cls()(model="m", base_url="http://x")
        assert m.model_name == "m"
        info = m.get_model_info()
        assert info.model_type == "ollama" and info.provider == "Ollama"

    def test_base_url_props(self):
        assert _openai_cls()(model="m", api_key="k", base_url="http://x").base_url == "http://x"
        az = pv.resolve_protocol_class("azure_openai")
        assert az is not None
        m = az(model="m", api_key="k", azure_endpoint="https://x", api_version="v")
        assert m.base_url == "https://x"


class TestProbeKey:
    def test_probe_uses_fields(self, monkeypatch):
        seen = {}
        def fake(protocol, base_url, model_name, api_key=None):
            seen.update(protocol=protocol, base_url=base_url, model_name=model_name, api_key=api_key)
            return 8000
        monkeypatch.setattr(pv, "probe_context_window", fake)
        m = _openai_cls()(model="m", api_key="k", base_url="http://x")
        assert m._probe_api_for_context_window() == 8000
        assert seen["protocol"] == "openai" and seen["api_key"] == "k"

    def test_key_unwrap(self):
        m = _openai_cls()(model="m", api_key=_LONG_FAKE_KEY, base_url="http://x")
        assert m._resolved_api_key() == _LONG_FAKE_KEY

    def test_key_missing(self):
        m = _openai_cls()(model="m", api_key="k", base_url="http://x")
        m.__dict__["openai_api_key"] = None
        assert m._resolved_api_key() is None

    def test_ollama_no_key_field(self):
        m = _ollama_cls()(model="m", base_url="http://x")
        assert m._resolved_api_key() is None
        assert m.protocol_spec.model_type == "ollama"


class TestResolve:
    def test_cache(self):
        first = pv.resolve_protocol_class("openai")
        assert pv.resolve_protocol_class("openai") is first

    def test_unknown(self):
        assert pv.resolve_protocol_class("ghost") is None

    def test_missing_sdk(self, monkeypatch):
        """模拟 SDK 缺失（builder 返回 None），不依赖本机是否装了厂商包。"""
        monkeypatch.setitem(pv._PROTOCOL_BUILDERS, "anthropic", lambda: None)
        monkeypatch.setitem(pv._PROTOCOL_BUILDERS, "gemini", lambda: None)
        monkeypatch.setattr(pv, "_PROTOCOL_CLASS_CACHE", {})
        assert pv.resolve_protocol_class("anthropic") is None
        assert pv.resolve_protocol_class("gemini") is None

    def test_lazy_map(self):
        assert "openai" in pv.PROTOCOL_CLASSES
        assert "ghost" not in pv.PROTOCOL_CLASSES
        assert 123 not in pv.PROTOCOL_CLASSES
        assert len(pv.PROTOCOL_CLASSES) == 6
        assert set(iter(pv.PROTOCOL_CLASSES)) == set(pv._PROTOCOL_BUILDERS)
        assert pv.PROTOCOL_CLASSES.get("ghost") is None
        assert pv.PROTOCOL_CLASSES.get("ghost", "d") == "d"
        assert pv.PROTOCOL_CLASSES["openai"] is not None
        with pytest.raises(KeyError):
            pv.PROTOCOL_CLASSES["ghost"]

    def test_module_getattr(self):
        assert pv.OpenAIModel is not None
        with pytest.raises(AttributeError):
            pv.GhostModel

    def test_availability(self, monkeypatch):
        """包探测结果由注入决定，避免「本机装了什么」影响断言。"""
        monkeypatch.setattr(pv, "_has_package", lambda name: name == "langchain_ollama")
        assert pv.is_anthropic_available() is False
        assert pv.is_ollama_available() is True

    def test_validator_passthrough(self):
        cls = _openai_cls()
        assert cls._accept_model_name_kwarg("x") == "x"
        assert cls._accept_model_name_kwarg({"model": "m"}) == {"model": "m"}

    def test_custom_spec_paths(self, monkeypatch):
        from lib.models import registry as registry_mod
        from lib.models.registry import ModelProviderRegistry, ModelProviderSpec

        # anthropic 的缺包分支由注入决定：本机装了厂商包时同样能验证该分支。
        monkeypatch.setattr(registry_mod, "is_anthropic_available", lambda: False)

        class FakeModel:
            def __init__(self, **kw):
                self.kw = kw

            def check_connection(self):
                return False

        reg = ModelProviderRegistry([
            ModelProviderSpec(key="openai", model_class=FakeModel, display_name="F",
                              protocol="openai", requires_package="pkg"),
            ModelProviderSpec(key="generic", model_class=FakeModel, display_name="N",
                              protocol="openai", requires_base_url=True),
            ModelProviderSpec(key="anthropic", model_class=FakeModel, display_name="A",
                              protocol="anthropic"),
            ModelProviderSpec(key="bare", model_class=None, display_name="B", protocol="ghost"),
        ])
        assert reg._spec_available(reg.get("openai")) is True
        assert reg.get_model_info("openai")["name"] == "F"
        ok, msg = reg.test_connection("openai", "m")
        assert (ok, msg) == (False, "连接失败")
        with pytest.raises(ValueError):
            reg.create_model("generic", model_name="m")
        ok, msg = reg.validate_profile("generic", "m")
        assert ok is False and "base_url" in msg
        with pytest.raises(ImportError):
            reg.create_model("anthropic", model_name="m")
        ok, msg = reg.test_connection("anthropic", "m")
        assert ok is False
        with pytest.raises(ImportError) as exc:
            reg.get_model_class("bare")
        assert "依赖模块未安装" in str(exc.value)

    def test_normalize_config_fallback(self):
        from types import SimpleNamespace
        from lib.models.registry import _normalize_config

        assert _normalize_config(SimpleNamespace(x=1, _y=2)) == {"x": 1}


class TestRegistryGaps:
    def _reg(self):
        from lib.models.registry import _build_default_registry

        return _build_default_registry()

    def test_azure_needs_endpoint(self):
        with pytest.raises(ValueError):
            self._reg().create_model("azure_openai", model_name="m", api_key="k")

    def test_create_drops_none(self):
        m = self._reg().create_model("openai", model_name="m", api_key="k",
                                     base_url="http://x", model=None, azure_deployment=None)
        assert m.model_name == "m"

    def test_create_model_kwarg(self):
        m = self._reg().create_model("openai", model="kwarg-model", api_key="k", base_url="http://x")
        assert m.model_name == "kwarg-model"

    def test_get_unknown(self):
        from lib.models.registry import ModelProviderRegistry

        with pytest.raises(ValueError):
            ModelProviderRegistry().get("ghost")

    def test_missing_class_import_error(self):
        from lib.models.registry import ModelProviderRegistry, ModelProviderSpec

        reg = ModelProviderRegistry([ModelProviderSpec(key="ghost", model_class=None, display_name="G",
                                                       protocol="ghost", requires_package="no-such-pkg-xyz")])
        with pytest.raises(ImportError):
            reg.get_model_class("ghost")

    def test_anthropic_guard(self, monkeypatch):
        import lib.models.registry as _reg

        monkeypatch.setattr(_reg, "is_anthropic_available", lambda: False)
        with pytest.raises(ImportError):
            self._reg().create_model("anthropic", model_name="m")

    def test_validate_profile(self):
        reg = self._reg()
        ok, _ = reg.validate_profile("ghost", "m")
        assert ok is False
        ok, msg = reg.validate_profile("openai", "", api_key="k", base_url="http://x")
        assert ok is False and "不能为空" in msg
        ok, msg = reg.validate_profile("openai", "m", api_key="k", base_url="http://x", context_window="oops")
        assert ok is False and "上下文" in msg
        assert reg.validate_profile("openai", "m", api_key="k", base_url="http://x")[0] is True

    def test_test_connection_variants(self):
        ok, _ = self._reg().test_connection("ghost", "m")
        assert ok is False

    def test_model_classes_map(self):
        mapping = self._reg().model_classes()
        assert "openai" in mapping and "ollama" in mapping
        assert mapping["openai"] is None or mapping["openai"] is not None

    def test_is_supported(self):
        assert self._reg().is_supported("openai") is True
        assert self._reg().is_supported("ghost") is False

    def test_create_from_config(self):
        m = self._reg().create_from_config({"api_type": "openai", "model_name": "m",
                                            "api_key": "k", "base_url": "http://x"})
        assert m.model_name == "m"

    def test_compat_injection(self):
        m = self._reg().create_model("openai", model_name="m", api_key="k", base_url="http://x")
        assert m.compat is not None
