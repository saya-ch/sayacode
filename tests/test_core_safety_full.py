# core/safety.py 全覆盖：SafetyChecker 所有分支。

from pathlib import Path

import pytest

from lib.core.safety import Operation, SafetyChecker, SafetyLevel


def _checker(**kw):
    # 默认关闭自动拦截，便于逐分支断言（除非用例另有指定）。
    kw.setdefault("auto_block_critical", False)
    return SafetyChecker(**kw)


class TestCheckAndConfirm:
    def test_critical_autoblock(self):
        c = SafetyChecker(auto_block_critical=True)
        assert c.check_and_confirm("delete", "/etc/passwd", SafetyLevel.CRITICAL) is False

    def test_critical_no_autoblock_callback(self):
        c = _checker(callback_confirm=lambda op, t: True)
        assert c.check_and_confirm("delete", "x", SafetyLevel.CRITICAL) is True

    def test_callback_deny(self):
        c = _checker(callback_confirm=lambda op, t: False)
        assert c.check_and_confirm("write", "x") is False

    def test_default_deny(self):
        assert _checker().check_and_confirm("write", "x") is False


class TestFileOperation:
    def test_empty_path(self):
        ok, msg = _checker().check_file_operation("read", "")
        assert ok is False and "不能为空" in msg

    def test_system_dir(self):
        ok, _ = _checker().check_file_operation("read", "/etc/passwd")
        assert ok is False

    def test_execute_danger_ext(self):
        ok, msg = _checker().check_file_operation("execute", "run.exe")
        assert ok is False and ".exe" in msg

    def test_execute_safe_ext(self):
        ok, _ = _checker().check_file_operation("execute", "run.py")
        assert ok is True

    def test_delete_danger_file(self):
        ok, _ = _checker().check_file_operation("delete", "/etc/passwd")
        assert ok is False

    def test_delete_sensitive_file(self):
        ok, _ = _checker().check_file_operation("delete", "proj/.env")
        assert ok is False

    def test_delete_too_big(self, monkeypatch):
        import lib.core.safety as _safety

        monkeypatch.setattr(_safety, "check_delete_danger", lambda p: (False, "too big"))
        ok, msg = _checker().check_file_operation("delete", "notes.txt")
        assert ok is False and "too big" in msg

    def test_delete_big_dir(self, tmp_path):
        d = tmp_path / "big"
        d.mkdir()
        for i in range(21):
            (d / f"f{i}.txt").write_text("x", encoding="utf-8")
        ok, msg = _checker().check_file_operation("delete", str(d))
        assert ok is False and "21" in msg

    def test_delete_iterdir_fails(self, tmp_path, monkeypatch):
        from unittest import mock

        d = tmp_path / "d"
        d.mkdir()
        with mock.patch.object(Path, "iterdir", side_effect=OSError("busy")):
            ok, _ = _checker().check_file_operation("delete", str(d))
        assert ok is True

    def test_write_system_path(self):
        ok, msg = _checker().check_file_operation("write", "/etc/new.conf")
        assert ok is False and "系统保护目录" in msg

    def test_write_ok(self, tmp_path):
        ok, msg = _checker().check_file_operation("write", str(tmp_path / "a.txt"))
        assert (ok, msg) == (True, "文件操作安全")


class TestCommand:
    def test_empty(self):
        ok, _ = _checker().check_command("")
        assert ok is False

    def test_danger_blocked(self):
        ok, _ = _checker().check_command("rm -rf /")
        assert ok is False

    def test_danger_allowed_flag(self):
        c = _checker(allow_dangerous_commands=True)
        ok, msg = c.check_command("rm -rf /")
        assert ok is True and "警告" in msg

    def test_whitelist(self):
        ok, msg = _checker().check_command("git status")
        assert ok is True and "白名单" in msg

    def test_safe_passthrough(self):
        ok, msg = _checker().check_command("echo hello")
        assert (ok, msg) == (True, "命令安全")


class TestBatch:
    def test_empty(self):
        assert _checker().check_batch([], "delete") == (True, "无文件需要操作")

    def test_too_many(self):
        ok, msg = _checker().check_batch([f"f{i}" for i in range(101)], "delete")
        assert ok is False and "100" in msg

    def test_passthrough(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        ok, _ = _checker().check_batch([str(f)], "read")
        assert ok is True


class TestProtectedAndTraversal:
    def test_protected_env(self):
        ok, _ = _checker().check_write_to_protected_file("/home/u/.env")
        assert ok is False

    def test_protected_git_config(self):
        ok, _ = _checker().check_write_to_protected_file("/r/.git/config")
        assert ok is False

    def test_protected_ssh(self):
        ok, _ = _checker().check_write_to_protected_file("/home/u/.ssh/key")
        assert ok is False

    def test_unprotected(self):
        assert _checker().check_write_to_protected_file("main.py") == (True, "文件不在受保护列表中")

    def test_traversal_dotdot(self):
        ok, _ = _checker().check_path_traversal("../evil")
        assert ok is False

    def test_traversal_encoded(self):
        ok, _ = _checker().check_path_traversal("a%2e%2eb")
        assert ok is False

    def test_absolute_outside(self, tmp_path):
        import sys as _sys

        c = SafetyChecker(workspace_root=tmp_path)
        outside = "C:/Windows/System32/evil.txt" if _sys.platform.startswith("win") else "/etc/passwd"
        ok, _ = c.check_path_traversal(outside)
        assert ok is False

    def test_absolute_inside(self, tmp_path):
        c = SafetyChecker(workspace_root=tmp_path)
        assert c.check_path_traversal(str(tmp_path / "a")) == (True, "路径安全")

    def test_relative_safe(self):
        assert _checker().check_path_traversal("src/main.py") == (True, "路径安全")


class TestRiskLevel:
    def test_critical_path(self):
        assert _checker().get_operation_risk_level("read", "/etc/passwd") == SafetyLevel.CRITICAL

    def test_critical_command(self):
        assert _checker().get_operation_risk_level("run", "rm -rf /") == SafetyLevel.CRITICAL

    def test_system_path_is_critical_first(self):
        assert _checker().get_operation_risk_level("delete", "/etc/passwd") == SafetyLevel.CRITICAL

    def test_delete_high_risk(self):
        assert _checker().get_operation_risk_level("delete", "proj/.env") == SafetyLevel.HIGH_RISK

    def test_delete_safe_file(self):
        assert _checker().get_operation_risk_level("delete", "notes.txt") == SafetyLevel.SAFE

    def test_batch_delete(self):
        assert _checker().get_operation_risk_level("batch_delete", "x") == SafetyLevel.HIGH_RISK

    def test_execute_danger_ext(self):
        assert _checker().get_operation_risk_level("execute", "run.exe") == SafetyLevel.HIGH_RISK

    def test_write_protected(self):
        assert _checker().get_operation_risk_level("write", "/x/.env") == SafetyLevel.MEDIUM_RISK

    def test_default_safe(self):
        assert _checker().get_operation_risk_level("read", "notes.txt") == SafetyLevel.SAFE


class TestWarningAndConfirm:
    @pytest.mark.parametrize("level", ["critical", "high_risk", "medium_risk", "safe"])
    def test_all_levels(self, level):
        out = _checker().generate_warning_message("delete", "x", level)
        assert "delete" in out and "x" in out

    def test_critical_blocked_prints(self, capsys):
        c = SafetyChecker(auto_block_critical=True)
        assert c.request_confirmation("delete", "/etc/passwd") is False
        assert "delete" in capsys.readouterr().out

    def test_callback_confirm(self):
        c = _checker(callback_confirm=lambda op, t: True)
        assert c.request_confirmation("write", "notes.txt") is True

    def test_no_callback_deny(self, capsys):
        assert _checker().request_confirmation("write", "notes.txt") is False

    def test_custom_warning(self):
        c = _checker(callback_confirm=lambda op, t: True)
        assert c.request_confirmation("write", "x", warning_message="custom") is True


class TestHistory:
    def test_log_and_trim(self):
        c = _checker()
        for i in range(1001):
            c.log_operation("write", f"f{i}")
        assert len(c.operation_history) == 500

    def test_get_filter_limit(self):
        c = _checker()
        c.log_operation("write", "a", confirmed=True)
        c.log_operation("read", "b")
        assert len(c.get_operation_history("write")) == 1
        assert len(c.get_operation_history(limit=1)) == 1
        assert c.operation_history[0].confirmed is True

    def test_clear(self):
        c = _checker()
        c.log_operation("write", "a")
        c.clear_history()
        assert c.get_operation_history() == []

    def test_operation_defaults(self):
        op = Operation(type="read", target="x")
        assert op.details == "" and op.confirmed is False
