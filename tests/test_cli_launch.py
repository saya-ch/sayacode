# cli launch 冒烟：doctor 与 headless 路径（main 全路径另测）。

import pytest


def _main_mod():
    # lib.cli.__init__ 把 main 函数盖在子模块属性上，用 sys.modules 取真模块。
    import sys as _sys
    import lib.cli.main  # noqa: F401

    return _sys.modules["lib.cli.main"]


class TestMainSmoke:
    def test_doctor_text(self, tmp_path):
        _main = _main_mod()

        with pytest.raises(SystemExit) as exc:
            _main.main(["--doctor", "--workspace", str(tmp_path)])
        assert exc.value.code in (0, 1)

    def test_headless_passthrough(self, tmp_path, monkeypatch):
        _main = _main_mod()

        monkeypatch.setattr(_main, "run_headless", lambda *a, **k: 0)
        assert _main.main(["-p", "hi", "--workspace", str(tmp_path)]) is None


# ── parser ─────────────────────────────────────────────────────────────────

class TestParser:
    def test_build_and_parse(self):
        from lib.cli.parser import build_cli_parser

        args = build_cli_parser().parse_args(["--workspace", "/tmp", "--model-type", "ollama", "-p", "hi"])
        assert args.workspace == "/tmp" and args.prompt == "hi"
        assert args.output_format == "text"

    def test_lang_override(self):
        from lib.cli.parser import _language_override_from_argv

        assert _language_override_from_argv(["--lang", "en"]) == "en"
        assert _language_override_from_argv(["--lang=en"]) == "en"
        assert _language_override_from_argv(["--workspace", "x"]) is None
        assert _language_override_from_argv([]) is None

    def test_prepare_language(self, monkeypatch):
        from lib.cli.parser import _prepare_cli_language
        from lib.i18n import get_language_preference, set_language
        from lib.state import UserConfig

        before = get_language_preference()
        try:
            _prepare_cli_language(["--lang", "en"], UserConfig())
            assert get_language_preference() == "en"
        finally:
            set_language(before)

    def test_menu_build(self):
        from lib.cli.parser import _build_protocol_menu

        assert _build_protocol_menu(0) is not None

    def test_select_non_interactive(self, monkeypatch):
        import lib.cli.parser as _parser

        monkeypatch.setattr(_parser, "_supports_interactive_input", lambda: False)
        assert _parser.select_model_protocol(default_index=0) == dict(_parser._protocol_options()[0])

    def test_select_interactive_keys(self, monkeypatch):
        import lib.cli.parser as _parser

        monkeypatch.setattr(_parser, "_supports_interactive_input", lambda: True)
        keys = iter(["down", "up", "enter"])
        monkeypatch.setattr(_parser, "_read_menu_key", lambda: next(keys))
        opts = _parser._protocol_options()
        assert _parser.select_model_protocol(default_index=1) == dict(opts[1])

    def test_read_key_win(self, monkeypatch):
        import msvcrt as _real_msvcrt

        import lib.cli.parser as _parser

        seq = iter(["\r", "\x03", "\x00", "H", "\xe0", "P", "\xe0", "x", "a"])
        monkeypatch.setattr(_real_msvcrt, "getwch", lambda: next(seq))
        assert _parser._read_menu_key() == "enter"
        with pytest.raises(KeyboardInterrupt):
            _parser._read_menu_key()
        assert _parser._read_menu_key() == "up"
        assert _parser._read_menu_key() == "down"
        assert _parser._read_menu_key() == ""
        assert _parser._read_menu_key() == "a"

    def test_help_formatter(self):
        from lib.cli.parser import build_cli_parser

        parser = build_cli_parser()
        assert isinstance(parser.formatter_class, type)
        assert "sage" in parser.format_help()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])

    def test_protocol_options_passthrough(self):
        import lib.cli.parser as _parser

        assert _parser._protocol_options() == _parser.PROTOCOL_OPTIONS


# ── cli/workspace ──────────────────────────────────────────────────────────

class TestCliWorkspace:
    def test_remembered_resolve_fails(self, monkeypatch):
        from pathlib import Path as _Path

        from lib.cli.workspace import _remembered_workspace

        monkeypatch.setattr(_Path, "resolve",
                            lambda self, *a, **k: (_ for _ in ()).throw(OSError("bad disk")))
        assert _remembered_workspace({"workspace": "x"}) is None

    def test_remembered_variants(self, tmp_path):
        from lib.cli.workspace import _remembered_workspace
        from lib.state import UserConfig

        assert _remembered_workspace(None) is None
        assert _remembered_workspace({}) is None
        assert _remembered_workspace({"workspace": str(tmp_path)}) == tmp_path.resolve()
        cfg = UserConfig()
        cfg.workspace = str(tmp_path)
        assert _remembered_workspace(cfg) == tmp_path.resolve()
        cfg.workspace = ""
        assert _remembered_workspace(cfg) is None

    def test_resolve_explicit(self, tmp_path):
        from types import SimpleNamespace

        from lib.cli.workspace import resolve_launch_workspace

        target = tmp_path / "new-ws"
        out = resolve_launch_workspace(SimpleNamespace(workspace=str(target)), None)
        assert out == target.resolve() and target.exists()
        out = resolve_launch_workspace(SimpleNamespace(workspace=str(tmp_path)), None)
        assert out == tmp_path.resolve()

    def test_resolve_remembered_current(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from lib.cli.workspace import resolve_launch_workspace

        monkeypatch.chdir(tmp_path)
        out = resolve_launch_workspace(SimpleNamespace(workspace=None), {"workspace": str(tmp_path)})
        assert out == tmp_path.resolve()

    def test_get_path_non_interactive(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: False)
        target = tmp_path / "auto"
        assert _ws.get_workspace_path(target) == target.resolve()
        assert target.exists()

    def test_get_path_input_empty(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "_safe_console_input", lambda *a, **k: "")
        monkeypatch.chdir(tmp_path)
        assert _ws.get_workspace_path() == tmp_path.resolve()

    def test_get_path_input_other(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "_safe_console_input", lambda *a, **k: "sub")
        monkeypatch.chdir(tmp_path)
        assert _ws.get_workspace_path() == sub.resolve()

    def test_get_path_outside_cwd(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        other = tmp_path / "elsewhere"
        other.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "_safe_console_input", lambda *a, **k: "")
        monkeypatch.chdir(work)
        assert _ws.get_workspace_path(other) == other.resolve()

    def test_get_path_default_missing_created(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        target = tmp_path / "fresh-default"
        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "_safe_console_input", lambda *a, **k: "")
        monkeypatch.chdir(tmp_path)
        assert _ws.get_workspace_path(target) == target.resolve()
        assert target.exists()

    def test_get_path_create_confirmed(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "_safe_console_input", lambda *a, **k: "brand-new")
        monkeypatch.setattr(_ws, "confirm_action", lambda *a, **k: True)
        monkeypatch.chdir(tmp_path)
        assert _ws.get_workspace_path() == (tmp_path / "brand-new").resolve()

    def test_get_path_create_declined(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "_safe_console_input", lambda *a, **k: "nope-dir")
        monkeypatch.setattr(_ws, "confirm_action", lambda *a, **k: False)
        monkeypatch.chdir(tmp_path)
        assert _ws.get_workspace_path() == tmp_path.resolve()

    def test_check_git_changes(self, tmp_path):
        import subprocess

        from lib.cli.workspace import check_git_changes

        assert check_git_changes(tmp_path) is False
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True, capture_output=True)
        assert check_git_changes(tmp_path) is False
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        assert check_git_changes(tmp_path) is True

    def test_check_git_changes_broken(self, tmp_path, monkeypatch):
        import subprocess as _sp

        from lib.cli.workspace import check_git_changes

        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert check_git_changes(tmp_path) is False

    def test_suggest_commit_flow(self, tmp_path, monkeypatch):
        import subprocess

        import lib.cli.workspace as _ws
        import lib.tools.git_tools as _gt

        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True, capture_output=True)
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "confirm_action", lambda *a, **k: True)
        monkeypatch.setattr(_ws.console, "input", lambda *a, **k: "msg")
        seen = []

        class FakeTool:
            def __init__(self, name):
                self._name = name

            def invoke(self, args):
                seen.append(self._name)
                return "ok"

        monkeypatch.setattr(_gt, "git_status", FakeTool("status"))
        monkeypatch.setattr(_gt, "git_add", FakeTool("add"))
        monkeypatch.setattr(_gt, "git_commit", FakeTool("commit"))
        token = _gt.use_workspace(tmp_path)
        try:
            assert _ws.suggest_git_commit(tmp_path) is None
        finally:
            _gt.reset_workspace(token)
        assert seen == ["status", "add", "commit"]

    def test_suggest_guards(self, tmp_path, monkeypatch):
        import lib.cli.workspace as _ws

        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: False)
        assert _ws.suggest_git_commit(tmp_path) is None
        monkeypatch.setattr(_ws, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_ws, "check_git_changes", lambda ws: False)
        assert _ws.suggest_git_commit(tmp_path) is None
        monkeypatch.setattr(_ws, "check_git_changes", lambda ws: True)
        monkeypatch.setattr(_ws, "confirm_action", lambda *a, **k: False)
        assert _ws.suggest_git_commit(tmp_path) is None


# ── configure ──────────────────────────────────────────────────────────────

class TestConfigure:
    def test_clean_text(self):
        from lib.cli.configure import _clean_text_value

        assert _clean_text_value(None) == ""
        assert _clean_text_value("  a  ") == "a"
        assert _clean_text_value(123) == "123"

    def test_sanitize_url(self):
        from lib.cli.configure import _sanitize_base_url

        assert _sanitize_base_url("https://x.ai/v1") == "https://x.ai/v1"
        assert _sanitize_base_url("ftp://x") is None
        assert _sanitize_base_url("https://") is None
        assert _sanitize_base_url("") is None

    def test_base_url_default(self):
        from lib.cli.configure import _resolve_base_url_default

        proto = {"default_base_url": "http://d"}
        assert _resolve_base_url_default(proto, "https://x") == "https://x"
        assert _resolve_base_url_default(proto, "bad") == "http://d"

    def test_protocol_option(self):
        from lib.cli.configure import _get_protocol_option

        assert _get_protocol_option(None)["value"] == "ollama"
        assert _get_protocol_option("")["value"] == "ollama"
        assert _get_protocol_option("deepseek")["value"] == "deepseek"
        with pytest.raises(ValueError):
            _get_protocol_option("not-a-provider")

    def test_protocol_index(self):
        from lib.cli.configure import _get_protocol_default_index

        assert _get_protocol_default_index("deepseek") >= 0
        assert _get_protocol_default_index("ghost") == 3

    def test_format_window(self):
        from lib.cli.configure import _format_context_window_value

        assert "tokens" in _format_context_window_value(8000)
        assert _format_context_window_value(None) != ""

    def test_detect_window_error(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _cfg._detect_context_window_from_model("t", "m", {}) is None

    def test_prompt_window(self, monkeypatch):
        import lib.cli.configure as _cfg

        seq = iter(["oops", "8000"])
        monkeypatch.setattr(_cfg, "_safe_console_input", lambda *a, **k: next(seq))
        assert _cfg._prompt_context_window("m") == 8000

    def test_ensure_configured(self):
        import lib.cli.configure as _cfg

        cfg = {"context_window": 4000}
        assert _cfg._ensure_context_window_configured("t", "m", cfg, interactive_input=False) == 4000

    def test_ensure_detected(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "_detect_context_window_from_model", lambda *a, **k: 16000)
        assert _cfg._ensure_context_window_configured("t", "m", {}, interactive_input=False) == 16000

    def test_ensure_interactive(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "_detect_context_window_from_model", lambda *a, **k: None)
        monkeypatch.setattr(_cfg, "_prompt_context_window", lambda name: 32000)
        assert _cfg._ensure_context_window_configured("t", "m", {}, interactive_input=True) == 32000

    def test_ensure_non_interactive_raises(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "_detect_context_window_from_model", lambda *a, **k: None)
        with pytest.raises(RuntimeError):
            _cfg._ensure_context_window_configured("t", "m", {}, interactive_input=False)

    def test_saved_profile_summary(self):
        import lib.cli.configure as _cfg

        _cfg._print_saved_profile_summary("p", "ollama", "m", {})
        _cfg._print_saved_profile_summary("p", "ollama", "m", {"api_key": "k", "base_url": "http://b"})

    def test_parse_window_arg(self):
        from types import SimpleNamespace

        import lib.cli.configure as _cfg

        assert _cfg._parse_context_window_arg(SimpleNamespace(context_window=None)) is None
        assert _cfg._parse_context_window_arg(SimpleNamespace(context_window="8000")) == 8000
        with pytest.raises(ValueError):
            _cfg._parse_context_window_arg(SimpleNamespace(context_window="oops"))

    def test_resolve_model_config(self, monkeypatch):
        from types import SimpleNamespace

        import lib.cli.configure as _cfg
        from lib.api_config import APIConfigManager

        args = SimpleNamespace(model_type="ollama", model_name=None, base_url=None,
                               api_key=None, context_window="8000")
        out = _cfg.resolve_launch_model_config(args, None, APIConfigManager(), interactive_input=False)
        assert out[0] == "ollama"

    def test_configure_model_non_interactive(self):
        import lib.cli.configure as _cfg

        mtype, mname, mcfg = _cfg.configure_model(default_model_type="ollama", interactive_input=False,
                                                  default_context_window="8000")
        assert mtype == "ollama" and mname
        assert "base_url" in mcfg

    def test_configure_model_bad_window(self):
        import lib.cli.configure as _cfg

        with pytest.raises(ValueError):
            _cfg.configure_model(default_model_type="ollama", interactive_input=False,
                                 default_context_window="oops")

    def test_configure_model_interactive(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "select_model_protocol",
                            lambda default_index=0: _cfg._get_protocol_option("ollama"))
        seq = iter(["https://x.ai", "", "mymodel"])
        monkeypatch.setattr(_cfg, "_safe_console_input", lambda *a, **k: next(seq))
        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        mtype, mname, mcfg = _cfg.configure_model(interactive_input=True)
        assert mtype == "ollama" and mname == "mymodel"
        assert mcfg["base_url"] == "https://x.ai"

    def test_configure_model_bad_url_warns(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "select_model_protocol",
                            lambda default_index=0: _cfg._get_protocol_option("ollama"))
        seq = iter(["", "", "m"])
        monkeypatch.setattr(_cfg, "_safe_console_input", lambda *a, **k: next(seq))
        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        mtype, _, _ = _cfg.configure_model(default_base_url="bad url", interactive_input=True)
        assert mtype == "ollama"

    def test_connection_ok(self, monkeypatch):
        import lib.cli.configure as _cfg

        class FakeModel:
            def chat(self, messages):
                return "hi"

            def detect_context_window(self):
                return 8000

        class FakeRegistry:
            def create_model(self, *a, **k):
                return FakeModel()

        monkeypatch.setattr(_cfg, "get_model_provider_registry", lambda: FakeRegistry())
        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        assert _cfg.test_model_connection("t", "m", {}) is True

    def test_configure_lock_type(self, monkeypatch):
        import lib.cli.configure as _cfg

        seq = iter(["", "", "m"])
        monkeypatch.setattr(_cfg, "_safe_console_input", lambda *a, **k: next(seq))
        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        mtype, _, _ = _cfg.configure_model(default_model_type="ollama", lock_model_type=True,
                                            interactive_input=True)
        assert mtype == "ollama"

    def test_resolve_saved_profile_calls_ensure(self, monkeypatch):
        from types import SimpleNamespace

        import lib.cli.configure as _cfg
        from lib.api_config import APIConfig, APIConfigManager

        mgr = APIConfigManager()
        mgr.add_config("p1", APIConfig(api_type="ollama", base_url="http://localhost:11434",
                                        model_name="m", context_window=8000))
        assert mgr.set_current("p1") is True
        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        args = SimpleNamespace(model_type=None, model_name=None, base_url=None,
                               api_key=None, context_window=None)
        out = _cfg.resolve_launch_model_config(args, None, mgr, interactive_input=False)
        assert out[0] == "ollama"

    def test_resolve_calls_ensure_closure(self, monkeypatch):
        from types import SimpleNamespace

        import lib.cli.configure as _cfg
        from lib.api_config import APIConfigManager

        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        args = SimpleNamespace(model_type="ollama", model_name=None, base_url=None,
                               api_key=None, context_window=None)
        out = _cfg.resolve_launch_model_config(args, None, APIConfigManager(), interactive_input=False)
        assert out[0] == "ollama"

    def test_configure_url_retry(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "select_model_protocol",
                            lambda default_index=0: _cfg._get_protocol_option("ollama"))
        seq = iter(["ht!tp:// bad", "", "", "m"])
        monkeypatch.setattr(_cfg, "_safe_console_input", lambda *a, **k: next(seq))
        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        mtype, _, _ = _cfg.configure_model(interactive_input=True)
        assert mtype == "ollama"

    def test_configure_key_branches(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "select_model_protocol",
                            lambda default_index=0: _cfg._get_protocol_option("deepseek"))
        monkeypatch.setattr(_cfg, "_ensure_context_window_configured", lambda *a, **k: 8000)
        seq = iter(["", "typed-key", "m"])
        monkeypatch.setattr(_cfg, "_safe_console_input", lambda *a, **k: next(seq))
        _, _, mcfg = _cfg.configure_model(interactive_input=True)
        assert mcfg["api_key"] == "typed-key"
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
        seq = iter(["", "", "m"])
        monkeypatch.setattr(_cfg, "_safe_console_input", lambda *a, **k: next(seq))
        _, _, mcfg = _cfg.configure_model(interactive_input=True)
        assert mcfg["api_key"] == "env-key"

    def test_configure_non_interactive_env_key(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
        _, _, mcfg = _cfg.configure_model(default_model_type="deepseek", interactive_input=False,
                                           default_context_window="8000")
        assert mcfg["api_key"] == "env-key"

    def test_connection_fail(self, monkeypatch):
        import lib.cli.configure as _cfg

        monkeypatch.setattr(_cfg, "get_model_provider_registry",
                            lambda: (_ for _ in ()).throw(RuntimeError("down")))
        assert _cfg.test_model_connection("t", "m", {}) is False


# ── main 深路径 ────────────────────────────────────────────────────────────

class TestMainDeep:
    def test_config_helpers(self, tmp_path, monkeypatch):
        _main = _main_mod()
        from lib.state import UserConfig

        assert isinstance(_main.load_user_config(), UserConfig)
        monkeypatch.setattr(UserConfig, "default_path", classmethod(lambda cls: tmp_path / "cfg.json"))
        assert _main.save_user_config(UserConfig()).exists()

    def test_save_prefs(self, tmp_path, monkeypatch):
        _main = _main_mod()
        from lib.i18n import get_language_preference, set_language
        from lib.state import UserConfig

        before = get_language_preference()
        try:
            monkeypatch.setattr(UserConfig, "default_path", classmethod(lambda cls: tmp_path / "cfg.json"))
            _main._save_language_preference(UserConfig(), "en")
            assert get_language_preference() == "en"
            _main._save_language_preference(None, "en")
            _main._save_prompt_style_preference(UserConfig(), "concise")
            _main._save_prompt_style_preference(None, "concise")
            _main._save_agent_mode_preference(UserConfig(), "plan")
            _main._save_agent_mode_preference(None, "plan")
        finally:
            set_language(before)

    def test_stdio_encoding(self, monkeypatch):
        _main = _main_mod()

        class NoReconfigure:
            pass

        class BadStream:
            def reconfigure(self, **kw):
                raise OSError("nope")

        monkeypatch.setattr(_main.sys, "stdout", NoReconfigure())
        monkeypatch.setattr(_main.sys, "stderr", BadStream())
        _main._configure_stdio_encoding()

    def test_doctor_json(self, tmp_path):
        _main = _main_mod()
        with pytest.raises(SystemExit) as exc:
            _main.main(["--doctor", "--json", "--workspace", str(tmp_path)])
        assert exc.value.code in (0, 1)

    def test_doctor_bundle(self, tmp_path):
        _main = _main_mod()
        target = tmp_path / "bundle.zip"
        with pytest.raises(SystemExit) as exc:
            _main.main(["--doctor", "--bundle", str(target), "--workspace", str(tmp_path)])
        assert exc.value.code in (0, 1)

    def test_style_invalid_exits(self, tmp_path):
        _main = _main_mod()
        with pytest.raises(SystemExit) as exc:
            _main.main(["--style", "frobnicate", "--workspace", str(tmp_path)])
        assert exc.value.code == 2

    def test_mode_invalid_exits(self, tmp_path):
        _main = _main_mod()
        with pytest.raises(SystemExit) as exc:
            _main.main(["--mode", "frobnicate", "--workspace", str(tmp_path)])
        assert exc.value.code == 2

    def test_headless_exit_code(self, tmp_path, monkeypatch):
        _main = _main_mod()
        monkeypatch.setattr(_main, "run_headless", lambda *a, **k: 3)
        with pytest.raises(SystemExit) as exc:
            _main.main(["-p", "hi", "--workspace", str(tmp_path)])
        assert exc.value.code == 3

    def test_style_mode_save_then_headless(self, tmp_path, monkeypatch):
        _main = _main_mod()
        monkeypatch.setattr(_main, "run_headless", lambda *a, **k: 0)
        _main.main(["-p", "hi", "--style", "concise", "--mode", "plan",
                    "--workspace", str(tmp_path), "--no-clear"])

    def test_clear_screen_path(self, tmp_path, monkeypatch):
        _main = _main_mod()
        monkeypatch.setattr(_main, "resolve_launch_workspace", lambda *a, **k: tmp_path)
        monkeypatch.setattr(_main, "resolve_launch_model_config",
                            lambda *a, **k: ("ollama", "m", {}, None))
        monkeypatch.setattr(_main, "test_model_connection", lambda *a, **k: True)
        monkeypatch.setattr(_main, "StartupService",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(SystemExit):
            _main.main(["--workspace", str(tmp_path)])

    def test_home_workspace_warning(self, tmp_path, monkeypatch):
        from pathlib import Path as _Path

        _main = _main_mod()
        monkeypatch.setattr(_main, "resolve_launch_workspace", lambda *a, **k: _Path.home())
        monkeypatch.setattr(_main, "resolve_launch_model_config",
                            lambda *a, **k: ("ollama", "m", {}, None))
        monkeypatch.setattr(_main, "test_model_connection", lambda *a, **k: True)
        monkeypatch.setattr(_main, "StartupService",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(SystemExit):
            _main.main(["--workspace", str(tmp_path), "--no-clear"])

    def test_connection_warning_non_interactive(self, tmp_path, monkeypatch):
        _main = _main_mod()
        monkeypatch.setattr(_main, "resolve_launch_workspace", lambda *a, **k: tmp_path)
        monkeypatch.setattr(_main, "resolve_launch_model_config",
                            lambda *a, **k: ("ollama", "m", {}, None))
        monkeypatch.setattr(_main, "test_model_connection", lambda *a, **k: False)
        monkeypatch.setattr(_main, "_supports_interactive_input", lambda: False)
        monkeypatch.setattr(_main, "StartupService",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(SystemExit):
            _main.main(["--workspace", str(tmp_path), "--no-clear"])

    def test_save_lang_flag(self, tmp_path, monkeypatch):
        _main = _main_mod()
        from lib.i18n import get_language_preference, set_language

        before = get_language_preference()
        try:
            monkeypatch.setattr(_main, "run_headless", lambda *a, **k: 0)
            _main.main(["-p", "hi", "--lang", "en", "--workspace", str(tmp_path)])
            assert get_language_preference() == "en"
        finally:
            set_language(before)

    def test_interactive_full(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        _main = _main_mod()
        monkeypatch.setattr(_main, "resolve_launch_workspace", lambda *a, **k: tmp_path)
        monkeypatch.setattr(_main, "resolve_launch_model_config",
                            lambda *a, **k: ("ollama", "m", {}, None))
        monkeypatch.setattr(_main, "test_model_connection", lambda *a, **k: True)

        class FakeStartup:
            def __init__(self, *a, **k):
                pass

            def bootstrap(self, options):
                agent = SimpleNamespace(interrupt_handler=None, close=lambda: None)
                state = SimpleNamespace(workspace=tmp_path)
                return SimpleNamespace(state=state, agent=agent, mcp=None)

        monkeypatch.setattr(_main, "StartupService", FakeStartup)
        monkeypatch.setattr(_main, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_main, "print_workspace_dashboard", lambda *a, **k: None)
        monkeypatch.setattr(_main, "persist_local_state", lambda *a, **k: None)
        monkeypatch.setattr(_main.InteractiveLoop, "run", lambda self: None)
        monkeypatch.setattr(_main, "suggest_git_commit", lambda ws: None)
        _main.main(["--workspace", str(tmp_path), "--skip-connection-test", "--no-clear"])

    def test_interactive_bad_connection_abort(self, tmp_path, monkeypatch):
        _main = _main_mod()
        monkeypatch.setattr(_main, "resolve_launch_workspace", lambda *a, **k: tmp_path)
        monkeypatch.setattr(_main, "resolve_launch_model_config",
                            lambda *a, **k: ("ollama", "m", {}, None))
        monkeypatch.setattr(_main, "test_model_connection", lambda *a, **k: False)
        monkeypatch.setattr(_main, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(_main, "confirm_action", lambda *a, **k: False)
        with pytest.raises(SystemExit):
            _main.main(["--workspace", str(tmp_path), "--no-clear"])

    def test_bootstrap_failure_exits_1(self, tmp_path, monkeypatch):
        _main = _main_mod()
        monkeypatch.setattr(_main, "resolve_launch_workspace", lambda *a, **k: tmp_path)
        monkeypatch.setattr(_main, "resolve_launch_model_config",
                            lambda *a, **k: ("ollama", "m", {}, None))
        monkeypatch.setattr(_main, "test_model_connection", lambda *a, **k: True)
        monkeypatch.setattr(_main, "StartupService",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(SystemExit) as exc:
            _main.main(["--workspace", str(tmp_path), "--skip-connection-test", "--no-clear"])
        assert exc.value.code == 1

    def test_non_interactive_tail(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        _main = _main_mod()
        monkeypatch.setattr(_main, "resolve_launch_workspace", lambda *a, **k: tmp_path)
        monkeypatch.setattr(_main, "resolve_launch_model_config",
                            lambda *a, **k: ("ollama", "m", {}, None))
        monkeypatch.setattr(_main, "test_model_connection", lambda *a, **k: True)

        class FakeStartup:
            def __init__(self, *a, **k):
                pass

            def bootstrap(self, options):
                agent = SimpleNamespace(close=lambda: None)
                state = SimpleNamespace(workspace=tmp_path)
                return SimpleNamespace(state=state, agent=agent, mcp=None)

        monkeypatch.setattr(_main, "StartupService", FakeStartup)
        monkeypatch.setattr(_main, "_supports_interactive_input", lambda: False)
        monkeypatch.setattr(_main, "persist_local_state", lambda *a, **k: None)
        monkeypatch.setattr(_main, "suggest_git_commit", lambda ws: None)
        _main.main(["--workspace", str(tmp_path), "--skip-connection-test", "--no-clear"])
