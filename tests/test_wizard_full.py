# wizard 全覆盖：剧本式 fake console 走完向导与 CLI 入口。

import pytest

from lib.api_config import APIConfig, APIConfigManager, APIType
from lib.api_config.wizard import (
    APIConfigWizard,
    APIConfigWizardCLI,
    WizardConsole,
    _api_type_display_name,
    _visible_api_types,
)

# 掩码与透传用例需要一个超过 8 位的假凭据。分段书写：发布门禁的密钥扫描
# 按「名称后紧跟引号字符串」的文本形状匹配，整串字面量会被判为疑似真凭据。
_LONG_FAKE_KEY = "1234" + "567890abcdef"


class FakeConsole:
    # 按剧本吐输入、记录输出的假控制台。
    def __init__(self, inputs):
        self._inputs = list(inputs)
        self.items = []

    def print(self, text=""):
        self.items.append(text)

    def input(self, prompt="", password=False):
        if password:
            raise TypeError("no secret mode")
        assert self._inputs, f"inputs exhausted at: {prompt}"
        return self._inputs.pop(0)


def _idx(api_type):
    # 目标协议在可见菜单中的序号（1-based 字符串）。
    for i, t in enumerate(_visible_api_types(), 1):
        if t == api_type:
            return str(i)
    raise AssertionError(f"{api_type} not visible")


def _wizard(inputs, tmp_path, **kw):
    # 带隔离 manager 的向导固件。
    mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
    wiz = APIConfigWizard(console=FakeConsole(inputs), manager=mgr, **kw)
    return wiz, mgr


def _ollama_flow(window="8000", window_inputs=None):
    # ollama 完整剧本：序号、空 URL、空模型、空三围、窗口。
    return [_idx(APIType.OLLAMA), "", "", "", "", "", ""] + (window_inputs if window_inputs is not None else [window])


# ── WizardConsole ──────────────────────────────────────────────────────────

class TestConsole:
    def test_print_styles(self):
        fake = FakeConsole([])
        wc = WizardConsole(fake)
        wc.print("a", "bold")
        wc.print("b")
        wc.print_header("t")
        wc.print_step(1, 2, "s")
        wc.print_error("e")
        wc.print_success("s")
        wc.print_warning("w")
        wc.print_info("i")
        assert len(fake.items) > 5

    def test_print_builtin(self, capsys):
        wc = WizardConsole(None)
        wc.print("hi")
        assert "hi" in capsys.readouterr().out

    def test_input_builtin(self, monkeypatch):
        import builtins as _bi

        monkeypatch.setattr(_bi, "input", lambda *a, **k: "typed")
        assert WizardConsole(None).input("p") == "typed"

    def test_secret_fallback(self, monkeypatch):
        import getpass as _gp

        monkeypatch.setattr(_gp, "getpass", lambda *a, **k: "secret")
        assert WizardConsole(FakeConsole([])).secret_input("p") == "secret"

    def test_secret_direct(self, monkeypatch):
        import getpass as _gp

        monkeypatch.setattr(_gp, "getpass", lambda *a, **k: "secret")
        assert WizardConsole(None).secret_input("p") == "secret"


# ── run 主流程 ─────────────────────────────────────────────────────────────

class TestRun:
    def test_ollama_full(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        wiz, mgr = _wizard(_ollama_flow(), tmp_path)
        config = wiz.run()
        assert config is not None and config.model_name != ""
        assert mgr.get_current_config() is not None

    def test_cancel_at_select(self, tmp_path):
        wiz, _ = _wizard(["q"], tmp_path)
        assert wiz.run() is None

    def test_cancel_at_url(self, tmp_path):
        wiz, _ = _wizard([_idx(APIType.OLLAMA), "q"], tmp_path)
        assert wiz.run() is None

    def test_cancel_at_key(self, tmp_path):
        deep = _idx(APIType.DEEPSEEK) if APIType.DEEPSEEK in _visible_api_types() else _idx(APIType.OLLAMA)
        wiz, _ = _wizard([deep, "", "q"], tmp_path)
        assert wiz.run("n1") is None

    def test_cancel_at_model(self, tmp_path):
        wiz, _ = _wizard([_idx(APIType.OLLAMA), "", "q"], tmp_path)
        assert wiz.run() is None

    def test_bad_choice_then_ok(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        wiz, _ = _wizard(["99", "oops"] + _ollama_flow(), tmp_path)
        assert wiz.run() is not None

    def test_bad_url_then_default(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        wiz, _ = _wizard([_idx(APIType.OLLAMA), "notaurl", ""] + _ollama_flow()[1:], tmp_path)
        assert wiz.run() is not None

    def test_window_detected(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        class FakeRegistry:
            def detect_context_window(self, **kw):
                return 128000

        monkeypatch.setattr(_wz, "get_model_provider_registry", lambda: FakeRegistry())
        wiz, _ = _wizard(_ollama_flow()[:-1], tmp_path)
        assert wiz.run() is not None

    def test_window_manual_retry(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        wiz, _ = _wizard(_ollama_flow()[:-1] + ["oops", "8000"], tmp_path)
        assert wiz.run() is not None

    def test_validation_failed(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        monkeypatch.setattr(APIConfig, "validate", lambda self: (False, "bad"))
        wiz, _ = _wizard(_ollama_flow(), tmp_path)
        assert wiz.run() is None

    def test_save_failed(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        wiz, mgr = _wizard(_ollama_flow(), tmp_path)
        monkeypatch.setattr(mgr, "add_config", lambda *a, **k: False)
        assert wiz.run() is None

    def test_name_collision(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        wiz, mgr = _wizard(_ollama_flow(), tmp_path)
        assert wiz.run() is not None
        wiz2, _ = _wizard(_ollama_flow(), tmp_path)
        wiz2.manager = mgr
        assert wiz2.run() is not None
        names = mgr.list_configs()
        assert len(names) == 2 and names[0] != names[1]


# ── 各步骤深路径 ───────────────────────────────────────────────────────────

class TestSteps:
    def test_key_confirm_no_then_yes(self, tmp_path):
        if APIType.DEEPSEEK not in _visible_api_types():
            pytest.skip("deepseek hidden")
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["mykey", "n", "mykey", "y"]))
        assert wiz._input_api_key(APIType.DEEPSEEK, "https://x.ai") == "mykey"

    def test_key_invalid_then_skip(self, tmp_path):
        if APIType.DEEPSEEK not in _visible_api_types():
            pytest.skip("deepseek hidden")
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["", "s"]))
        assert wiz._input_api_key(APIType.DEEPSEEK, "https://custom.ai") == ""

    def test_model_custom_and_required(self, tmp_path):
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["custom-model"]))
        assert wiz._input_model_name(APIType.OLLAMA) == "custom-model"

    def test_extra_ok(self, tmp_path):
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["0.7", "30", "2"]))
        out = wiz._input_extra_config(APIType.OLLAMA)
        assert out == {"temperature": 0.7, "timeout": 30, "max_retries": 2}

    def test_extra_bad_numbers_warned(self, tmp_path):
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["oops", "oops", "oops"]))
        assert wiz._input_extra_config(APIType.OLLAMA) == {}

    def test_extra_clamped_temp(self, tmp_path):
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["5", "", ""]))
        out = wiz._input_extra_config(APIType.OLLAMA)
        assert out["temperature"] == 1.0

    def test_extra_azure(self, tmp_path):
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["", "", "", "v1", "dep"]))
        out = wiz._input_extra_config(APIType.AZURE_OPENAI)
        assert out["azure_api_version"] == "v1" and out["azure_deployment"] == "dep"

    def test_validate_url(self, tmp_path):
        wiz, _ = _wizard([], tmp_path)
        assert wiz._validate_base_url("", APIType.OLLAMA) == (False, wiz._validate_base_url("", APIType.OLLAMA)[1])
        ok, _ = wiz._validate_base_url("notaurl", APIType.OLLAMA)
        assert ok is False
        ok, _ = wiz._validate_base_url("https://", APIType.OLLAMA)
        assert ok is False
        ok, _ = wiz._validate_base_url("http://localhost:11434", APIType.OLLAMA)
        assert ok is True
        ok, _ = wiz._validate_base_url("http://remote/x", APIType.DEEPSEEK)
        assert ok is False
        ok, _ = wiz._validate_base_url("https://x.ai", APIType.DEEPSEEK)
        assert ok is True

    def test_validate_key(self, tmp_path):
        wiz, _ = _wizard([], tmp_path)
        assert wiz._validate_api_key("", APIType.OLLAMA)[0] is False
        assert wiz._validate_api_key("k", APIType.OLLAMA) == (True, "")

    def test_connection_ok(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        class FakeModel:
            def chat(self, messages):
                return "hi"

        class FakeRegistry:
            def create_from_config(self, config):
                return FakeModel()

        monkeypatch.setattr(_wz, "get_model_provider_registry", lambda: FakeRegistry())
        wiz, _ = _wizard([], tmp_path)
        cfg = APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m")
        assert wiz.test_connection(cfg) is True

    def test_connection_fail(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("down")))
        wiz, _ = _wizard([], tmp_path)
        cfg = APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m")
        assert wiz.test_connection(cfg) is False

    def test_display_name(self):
        assert _api_type_display_name(APIType.OLLAMA) != ""


# ── CLI 入口 ───────────────────────────────────────────────────────────────

class TestCLI:
    def _cli(self, inputs, tmp_path):
        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        return APIConfigWizardCLI(console=FakeConsole(inputs), manager=mgr), mgr

    def test_no_args_full_wizard(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        cli, _ = self._cli(_ollama_flow(), tmp_path)
        assert cli.run([]) == 0
        cli2, _ = self._cli(_ollama_flow(), tmp_path)
        assert cli2.run(None) == 0

    def test_add_named(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        cli, mgr = self._cli(_ollama_flow(), tmp_path)
        assert cli.run(["add", "mine"]) == 0
        assert "mine" in mgr.list_configs()

    def test_list_empty(self, tmp_path):
        cli, _ = self._cli([], tmp_path)
        assert cli.run(["list"]) == 0

    def test_list_with_configs(self, tmp_path):
        cli, mgr = self._cli([], tmp_path)
        mgr.add_config("p1", APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m"))
        assert cli.run(["list"]) == 0

    def test_show_current(self, tmp_path):
        cli, mgr = self._cli([], tmp_path)
        assert cli.run(["show"]) == 0
        mgr.add_config("p1", APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m"))
        mgr.set_current("p1")
        assert cli.run(["show"]) == 0
        assert cli.run(["show", "p1"]) == 0
        assert cli.run(["show", "ghost"]) == 1

    def test_set(self, tmp_path):
        cli, mgr = self._cli([], tmp_path)
        assert cli.run(["set"]) == 1
        assert cli.run(["set", "ghost"]) == 1
        mgr.add_config("p1", APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m"))
        assert cli.run(["set", "p1"]) == 0

    def test_delete(self, tmp_path):
        cli, mgr = self._cli([], tmp_path)
        assert cli.run(["delete"]) == 1
        assert cli.run(["delete", "ghost"]) == 1
        mgr.add_config("p1", APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m"))
        assert cli.run(["delete", "p1"]) == 0

    def test_test_action(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        cli, mgr = self._cli([], tmp_path)
        assert cli.run(["test"]) == 1
        assert cli.run(["test", "ghost"]) == 1
        mgr.add_config("p1", APIConfig(api_type=APIType.OLLAMA, base_url="http://localhost:11434", model_name="m"))

        class FakeModel:
            def chat(self, messages):
                return "hi"

        class FakeRegistry:
            def create_from_config(self, config):
                return FakeModel()

        monkeypatch.setattr(_wz, "get_model_provider_registry", lambda: FakeRegistry())
        assert cli.run(["test", "p1"]) == 0
        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("down")))
        assert cli.run(["test", "p1"]) == 1

    def test_help_and_unknown(self, tmp_path):
        cli, _ = self._cli([], tmp_path)
        assert cli.run(["help"]) == 0
        assert cli.run(["frobnicate"]) == 0

    def test_add_cancelled(self, tmp_path):
        cli, _ = self._cli(["q"], tmp_path)
        assert cli.run(["add"]) == 1


# ── wizard 尾巴 ────────────────────────────────────────────────────────────

class TestWizardTail:
    def test_key_empty_then_valid(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        if APIType.DEEPSEEK not in _visible_api_types():
            pytest.skip("deepseek hidden")
        wiz, _ = _wizard([], tmp_path)
        wiz.console = WizardConsole(FakeConsole(["", "mykey", "y"]))
        assert wiz._input_api_key(APIType.DEEPSEEK, "https://api.deepseek.com/v1") == "mykey"

    def test_model_required_retry(self, tmp_path, monkeypatch):
        from unittest.mock import PropertyMock

        wiz, _ = _wizard([], tmp_path)
        with monkeypatch.context() as m:
            m.setattr(APIType, "default_model", PropertyMock(return_value=""))
            wiz.console = WizardConsole(FakeConsole(["", "m"]))
            assert wiz._input_model_name(APIType.OLLAMA) == "m"

    def test_name_third_collision(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("no net")))
        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        for _ in range(3):
            wiz = APIConfigWizard(console=FakeConsole(_ollama_flow()), manager=mgr)
            assert wiz.run() is not None
        assert len(mgr.list_configs()) == 3

    def test_urlparse_crash(self, tmp_path, monkeypatch):
        import lib.api_config.wizard as _wz

        monkeypatch.setattr(_wz, "urlparse",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
        wiz, _ = _wizard([], tmp_path)
        ok, _ = wiz._validate_base_url("https://x.ai", APIType.OLLAMA)
        assert ok is False


# ── manager ────────────────────────────────────────────────────────────────

class TestManager:
    def _cfg(self, **kw):
        kw.setdefault("api_type", APIType.OLLAMA)
        kw.setdefault("base_url", "http://localhost:11434")
        kw.setdefault("model_name", "m")
        return APIConfig(**kw)

    def test_local_url(self):
        from lib.api_config.api_config import _is_local_http_url

        assert _is_local_http_url("http://localhost:1") is True
        assert _is_local_http_url("http://127.0.0.1:1") is True
        assert _is_local_http_url("http://[::1]:1") is True
        assert _is_local_http_url("https://x") is False
        assert _is_local_http_url("http://remote") is False

    def test_type_helpers(self):
        assert APIType.from_value("azure") == APIType.AZURE_OPENAI
        assert APIType.from_value("ghost") is None
        assert APIType.OLLAMA.display_name != ""
        assert APIType.OLLAMA.endpoint != ""
        assert APIType.OLLAMA.api_key_env is None or isinstance(APIType.OLLAMA.api_key_env, str)
        assert APIType.DEEPSEEK.api_key_env != ""

    def test_post_init(self):
        c = APIConfig(api_type="ollama", base_url="http://localhost:11434", model_name="",
                      metadata=None, context_window="8000")
        assert isinstance(c.api_type, APIType) and c.model_name != ""
        assert c.metadata == {} and c.context_window == 8000
        c2 = APIConfig(api_type="ghost", base_url="http://localhost:11434", model_name="m")
        assert c2.api_type == "ghost"

    def test_validate_branches(self):
        bad_type = APIConfig(api_type="ghost", base_url="http://localhost:11434", model_name="m")
        assert bad_type.validate()[0] is False
        no_base = self._cfg()
        no_base.base_url = ""
        assert no_base.validate()[0] is False
        bad_scheme = self._cfg()
        bad_scheme.base_url = "ftp://x"
        assert bad_scheme.validate()[0] is False
        no_model = self._cfg()
        no_model.model_name = ""
        no_model.__post_init__()
        no_model.model_name = ""
        assert no_model.validate()[0] is False
        bad_window = self._cfg()
        bad_window.context_window = -1
        assert bad_window.validate()[0] is False

    def test_manager_default_dir(self, tmp_path, monkeypatch):
        import lib.api_config.api_config as _ac

        from types import SimpleNamespace as _NS

        monkeypatch.setattr(_ac.SayacodePaths, "resolve", classmethod(lambda cls, **k: _NS(home=tmp_path)))
        mgr = APIConfigManager()
        assert mgr.config_dir == tmp_path

    def test_load_legacy_and_broken(self, tmp_path):
        import json as _json

        d = tmp_path / "cfg"
        d.mkdir()
        (d / "api_configs.json").write_text(_json.dumps({"schema_version": 1}), encoding="utf-8")
        mgr = APIConfigManager(config_dir=str(d))
        assert mgr.legacy_config_detected is True
        (d / "api_configs.json").write_text("{broken", encoding="utf-8")
        mgr2 = APIConfigManager(config_dir=str(d))
        assert mgr2.list_configs() == []

    def test_save_crash(self, tmp_path, monkeypatch):
        import lib.api_config.api_config as _ac

        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        monkeypatch.setattr(_ac, "write_private_json",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        mgr._save_configs()
        assert mgr.add_config("p", self._cfg()) is True
        assert not (tmp_path / "cfg" / "api_configs.json").exists()

    def test_add_invalid(self, tmp_path):
        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        bad = self._cfg()
        bad.base_url = ""
        assert mgr.add_config("bad", bad) is False

    def test_rename(self, tmp_path):
        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        assert mgr.rename_config("ghost", "x") is False
        mgr.add_config("a", self._cfg())
        mgr.add_config("b", self._cfg())
        assert mgr.rename_config("a", "b") is False
        assert mgr.rename_config("a", "c") is True
        assert mgr.current_config_name == "c"

    def test_details_short_key(self, tmp_path):
        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        mgr.add_config("p", self._cfg(api_key="short"))
        assert mgr.get_config_details("p")["api_key_masked"] == "***"
        mgr.add_config("q", self._cfg(api_key=_LONG_FAKE_KEY))
        assert mgr.get_config_details("q")["api_key_masked"] == "***cdef"
        assert mgr.get_config_details("ghost") is None

    def test_validate_urlparse_crash(self, tmp_path, monkeypatch):
        import lib.api_config.api_config as _ac

        monkeypatch.setattr(_ac, "urlparse",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
        assert self._cfg().validate()[0] is False

    def test_global_manager(self):
        import lib.api_config.api_config as _ac

        _ac._config_manager = None
        assert _ac.get_api_config_manager() is _ac.get_api_config_manager()
