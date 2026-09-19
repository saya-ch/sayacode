# cli/permissions 全覆盖：格式化、按键、确认流、中断器、弹窗队列。

import pytest

import lib.cli.permissions as cp
from lib.core.permission_policy import PermissionRequest


def _req(tool="read_file", preview='{"path": "a.txt"}'):
    # 标准权限请求固件。
    return PermissionRequest(tool_name=tool, action="ask",
                             arguments_preview=preview, source="policy")


def _interactive(monkeypatch, keys):
    # 喂按键序列并强制交互可用。
    seq = iter(keys)
    monkeypatch.setattr(cp, "_supports_interactive_input", lambda: True)
    monkeypatch.setattr(cp, "_read_choice_key", lambda: next(seq))
    return seq


class TestFormatArgs:
    def test_bad_json(self):
        assert cp._format_permission_args("x", "{oops") == "{oops"

    def test_empty(self):
        assert cp._format_permission_args("x", "") == ""

    def test_write(self):
        out = cp._format_permission_args("write_file", '{"path": "a", "content_length": 3}')
        assert "a" in out

    def test_replace(self):
        assert "a" in cp._format_permission_args("search_replace", '{"path": "a"}')

    def test_delete(self):
        assert "a" in cp._format_permission_args("delete_file", '{"path": "a"}')

    def test_mkdir(self):
        assert "d" in cp._format_permission_args("create_directory", '{"path": "d"}')

    def test_exec(self):
        assert "echo" in cp._format_permission_args("execute_command_tool", '{"command": "echo hi"}')

    def test_git(self):
        out = cp._format_permission_args("git_add", '{"files": ["a"]}')
        assert "a" in out and "files" in out

    def test_read_output(self):
        assert "x" in cp._format_permission_args("read_output_file", '{"path": "x"}')

    def test_other(self):
        assert "k" in cp._format_permission_args("other_tool", '{"k": 1}')

    def test_panel(self):
        assert cp._build_confirm_panel("t", "ctx", 1) is not None


class TestInputHelpers:
    def test_supports(self):
        assert isinstance(cp._supports_interactive_input(), bool)

    def test_safe_input_eof(self, monkeypatch):
        from lib.cli import theme as _theme

        monkeypatch.setattr(_theme.console, "input", lambda *a, **k: (_ for _ in ()).throw(EOFError()))
        assert cp._safe_console_input("p", default="d") == "d"

    def test_secret_type_error_then_eof(self, monkeypatch):
        import getpass as _getpass
        from lib.cli import theme as _theme

        monkeypatch.setattr(_theme.console, "input",
                            lambda *a, **k: (_ for _ in ()).throw(TypeError("no pw")))
        monkeypatch.setattr(_getpass, "getpass",
                            lambda *a, **k: (_ for _ in ()).throw(EOFError()))
        assert cp._safe_secret_input("p", default="d") == "d"

    def test_choice_keys(self):
        assert cp._choice_from_key("y") == "once"
        assert cp._choice_from_key("1") == "once"
        assert cp._choice_from_key("a") == "session"
        assert cp._choice_from_key("2") == "session"
        assert cp._choice_from_key("s") == "save"
        assert cp._choice_from_key("p") == "save"
        assert cp._choice_from_key("3") == "save"
        assert cp._choice_from_key("n") == "deny"
        assert cp._choice_from_key("4") == "deny"
        assert cp._choice_from_key("esc") == "deny"
        assert cp._choice_from_key("escape") == "deny"
        assert cp._choice_from_key("q") is None

    def test_read_key_win(self, monkeypatch):
        import msvcrt as _real

        seq = iter(["\x03", "\x00", "H", "\x00", "P", "\x00", "z", "\r", "\x1b", "q"])
        monkeypatch.setattr(_real, "getwch", lambda: next(seq))
        with pytest.raises(KeyboardInterrupt):
            cp._read_choice_key()
        assert cp._read_choice_key() == "up"
        assert cp._read_choice_key() == "down"
        assert cp._read_choice_key() == ""
        assert cp._read_choice_key() == "enter"
        assert cp._read_choice_key() == "esc"
        assert cp._read_choice_key() == "q"


class TestConfirm:
    def test_non_interactive(self, monkeypatch):
        monkeypatch.setattr(cp, "_supports_interactive_input", lambda: False)
        assert cp._confirm_tool_permission(_req()) is False

    def test_once(self, monkeypatch):
        _interactive(monkeypatch, ["y"])
        assert cp._confirm_tool_permission(_req()) is True

    def test_enter_selects_first(self, monkeypatch):
        _interactive(monkeypatch, ["enter"])
        assert cp._confirm_tool_permission(_req()) is True

    def test_navigate_then_enter(self, monkeypatch):
        _interactive(monkeypatch, ["down", "down", "up", "enter"])
        assert cp._confirm_tool_permission(_req()) is True

    def test_other_key_ignored(self, monkeypatch):
        _interactive(monkeypatch, ["q", "y"])
        assert cp._confirm_tool_permission(_req()) is True

    def test_eof_denies(self, monkeypatch):
        monkeypatch.setattr(cp, "_supports_interactive_input", lambda: True)
        monkeypatch.setattr(cp, "_read_choice_key",
                            lambda: (_ for _ in ()).throw(EOFError()))
        assert cp._confirm_tool_permission(_req()) is False

    def test_session_grant(self, monkeypatch):
        _interactive(monkeypatch, ["a"])
        assert cp._confirm_tool_permission(_req("my_session_tool_xyz")) is True

    def test_session_dangerous_once_only(self, monkeypatch):
        _interactive(monkeypatch, ["a"])
        assert cp._confirm_tool_permission(_req("delete_file")) is True

    def test_save_grant_user_fallback(self, tmp_path, monkeypatch):
        _interactive(monkeypatch, ["s"])
        assert cp._confirm_tool_permission(_req("my_save_tool_xyz")) is True

    def test_save_project_scope(self, tmp_path, monkeypatch):
        from lib.core.permission_workspace import configure_permission_workspace

        configure_permission_workspace(tmp_path)
        try:
            _interactive(monkeypatch, ["s"])
            assert cp._confirm_tool_permission(_req("my_save_tool_xyz2")) is True
        finally:
            configure_permission_workspace(".")

    def test_save_dangerous_once_only(self, monkeypatch):
        _interactive(monkeypatch, ["s"])
        assert cp._confirm_tool_permission(_req("delete_file")) is True

    def test_deny(self, monkeypatch):
        cp.reset_denial_tracker()
        _interactive(monkeypatch, ["n"])
        assert cp._confirm_tool_permission(_req()) is False

    def test_deny_fallback_after_three(self, monkeypatch):
        from lib.core.permission_session import _active_runtime

        cp.reset_denial_tracker()
        _active_runtime().is_in_fallback = False
        for _ in range(3):
            _interactive(monkeypatch, ["n"])
            assert cp._confirm_tool_permission(_req()) is False
        assert _active_runtime().is_in_fallback is True
        cp.reset_denial_tracker()
        _active_runtime().is_in_fallback = False


class TestHandlers:
    def test_configure_callback(self):
        cp.configure_permission_confirmation(True)
        cp.configure_permission_confirmation(False)

    def test_interrupt_bad_payload(self):
        handler = cp.build_interrupt_handler()
        assert handler({}) == {"approved": False}
        assert handler({"kind": "other"}) == {"approved": False}
        assert handler("oops") == {"approved": False}

    def test_interrupt_ok(self, monkeypatch):
        from lib.core.middleware import INTERRUPT_TOOL_ASK

        monkeypatch.setattr(cp, "_confirm_tool_permission", lambda req: True)
        handler = cp.build_interrupt_handler()
        out = handler({"kind": INTERRUPT_TOOL_ASK, "tool": "t", "args_preview": "{}", "source": "p"})
        assert out == {"approved": True}

    def test_deny_handler(self):
        assert cp.build_deny_interrupt_handler()({}) == {"approved": False}

    def test_dialog_queue(self):
        q = cp.PermissionDialogQueue()
        assert q.has_pending is False and q.queue_size == 0
        assert q.dequeue() is None
        d = cp.PermissionDialog(tool_name="t", description="d")
        q.enqueue(d)
        assert q.has_pending is True and q.queue_size == 1
        assert q.dequeue() is d
        assert q.has_pending is False

    def test_reset_tracker(self):
        cp.reset_denial_tracker()
