# POSIX 原始终端按键：Windows 走 msvcrt，这里覆盖 termios/tty 路径。

import sys
from types import SimpleNamespace

import pytest

from lib.cli import ttykeys


class _FakeStdin:
    """按脚本吐字符的假 stdin；``pending`` 决定 select 是否报可读。"""

    def __init__(self, chars):
        self._chars = list(chars)

    def fileno(self):
        return 0

    def pending(self):
        return bool(self._chars)

    def read(self, _count=1):
        return self._chars.pop(0) if self._chars else ""


def _stub_sys(stdin, **attrs):
    """只带 stdin（可加 platform）的 sys 替身，避免全局打补丁。"""
    return SimpleNamespace(stdin=stdin, **attrs)


def _install_terminal(monkeypatch, stdin):
    """注入假 stdin 与假 termios/tty，让 POSIX 分支在任意平台可跑。"""
    calls = []
    fake_termios = SimpleNamespace(
        tcgetattr=lambda fd: ("old", fd),
        tcsetattr=lambda fd, when, settings: calls.append((fd, when, settings)),
        TCSADRAIN="drain",
    )
    fake_tty = SimpleNamespace(setraw=lambda fd: calls.append(("raw", fd)))
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setitem(sys.modules, "tty", fake_tty)
    monkeypatch.setattr(ttykeys, "sys", _stub_sys(stdin))
    monkeypatch.setattr(
        ttykeys,
        "select",
        SimpleNamespace(select=lambda r, w, x, t: ([stdin], [], []) if stdin.pending() else ([], [], [])),
    )
    return calls


def test_raw_terminal_restores_settings(monkeypatch):
    calls = _install_terminal(monkeypatch, _FakeStdin([]))
    with ttykeys.raw_terminal():
        pass
    assert calls == [("raw", 0), (0, "drain", ("old", 0))]


def test_raw_terminal_restores_on_exception(monkeypatch):
    calls = _install_terminal(monkeypatch, _FakeStdin([]))
    with pytest.raises(RuntimeError):
        with ttykeys.raw_terminal():
            raise RuntimeError("boom")
    assert calls[-1] == (0, "drain", ("old", 0))


def test_read_escape_tail_arrow_sequence(monkeypatch):
    _install_terminal(monkeypatch, _FakeStdin(["[", "A"]))
    assert ttykeys.read_escape_tail() == "[A"


def test_read_escape_tail_returns_empty_for_lone_escape(monkeypatch):
    """回归：单按 ESC 时没有任何后续字节，必须立即返回而不是阻塞等键。"""
    _install_terminal(monkeypatch, _FakeStdin([]))
    assert ttykeys.read_escape_tail() == ""

class TestMenuKeyPosix:
    """parser.POSIX 分支：Windows 分支已有用例，这里补齐被忽略的一半。"""

    def _prepare(self, monkeypatch, chars):
        import lib.cli.parser as parser

        stdin = _FakeStdin(chars)
        _install_terminal(monkeypatch, stdin)
        monkeypatch.setattr(parser, "os", SimpleNamespace(name="posix"))
        monkeypatch.setattr(parser, "sys", _stub_sys(stdin))
        return parser

    def test_enter_and_plain_char(self, monkeypatch):
        parser = self._prepare(monkeypatch, ["\r"])
        assert parser._read_menu_key() == "enter"
        parser = self._prepare(monkeypatch, ["a"])
        assert parser._read_menu_key() == "a"

    def test_arrow_keys(self, monkeypatch):
        parser = self._prepare(monkeypatch, ["\x1b", "[", "A"])
        assert parser._read_menu_key() == "up"
        parser = self._prepare(monkeypatch, ["\x1b", "[", "B"])
        assert parser._read_menu_key() == "down"

    def test_lone_escape_does_not_block(self, monkeypatch):
        """回归：单按 ESC 曾会阻塞等待两个后续按键。"""
        parser = self._prepare(monkeypatch, ["\x1b"])
        assert parser._read_menu_key() == ""

    def test_ctrl_c_interrupts(self, monkeypatch):
        parser = self._prepare(monkeypatch, ["\x03"])
        with pytest.raises(KeyboardInterrupt):
            parser._read_menu_key()


class TestChoiceKeyPosix:
    """permissions.POSIX 分支：同样只有 Windows 分支有覆盖。"""

    def _prepare(self, monkeypatch, chars):
        import lib.cli.permissions as permissions

        stdin = _FakeStdin(chars)
        _install_terminal(monkeypatch, stdin)
        monkeypatch.setattr(permissions, "sys", _stub_sys(stdin, platform="linux"))
        return permissions

    def test_enter_and_plain_char(self, monkeypatch):
        permissions = self._prepare(monkeypatch, ["\r"])
        assert permissions._read_choice_key() == "enter"
        permissions = self._prepare(monkeypatch, ["s"])
        assert permissions._read_choice_key() == "s"

    def test_arrows_and_escape(self, monkeypatch):
        permissions = self._prepare(monkeypatch, ["\x1b", "[", "A"])
        assert permissions._read_choice_key() == "up"
        permissions = self._prepare(monkeypatch, ["\x1b", "[", "B"])
        assert permissions._read_choice_key() == "down"
        permissions = self._prepare(monkeypatch, ["\x1b"])
        assert permissions._read_choice_key() == "esc"

    def test_ctrl_c_interrupts(self, monkeypatch):
        permissions = self._prepare(monkeypatch, ["\x03"])
        with pytest.raises(KeyboardInterrupt):
            permissions._read_choice_key()
