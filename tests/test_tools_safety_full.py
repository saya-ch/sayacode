# tools/safety.py 全覆盖：原语所有分支。

from pathlib import Path

import pytest

from lib.core.safety_rules import (
    SafetyResult,
    check_batch_operation,
    check_command_danger,
    check_delete_danger,
    check_file_danger,
    check_write_operation,
    filter_dangerous_chars,
    get_danger_level,
    sanitize_path,
)


class TestResult:
    def test_bool(self):
        assert bool(SafetyResult(True, False, "ok")) is True
        assert bool(SafetyResult(False, False, "x")) is False
        assert bool(SafetyResult(True, True, "x")) is False


class TestFileDanger:
    def test_exe(self):
        ok, msg = check_file_danger("run.exe")
        assert ok is False and ".exe" in msg

    def test_resolved_sensitive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: tmp_path / ".env")
        ok, _ = check_file_danger("a.txt")
        assert ok is False
        monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: Path("C:/Windows/System32/x"))
        ok, _ = check_file_danger("a.txt")
        assert ok is False

    def test_resolve_fails(self, monkeypatch):
        monkeypatch.setattr(Path, "resolve",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        ok, msg = check_file_danger("a.txt")
        assert ok is False and "系统保护" in msg

    def test_ok(self, tmp_path):
        assert check_file_danger(str(tmp_path / "a.txt"))[0] is True


class TestDeleteDanger:
    def test_missing(self, tmp_path):
        assert check_delete_danger(str(tmp_path / "nope"))[0] is True

    def test_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        assert check_delete_danger(str(f))[0] is True

    def test_big_tree(self, tmp_path):
        d = tmp_path / "big"
        d.mkdir()
        for i in range(101):
            (d / f"f{i}.txt").write_text("x", encoding="utf-8")
        ok, msg = check_delete_danger(str(d))
        assert ok is False and "101" in msg

    def test_rglob_permission(self, tmp_path, monkeypatch):
        d = tmp_path / "d"
        d.mkdir()
        monkeypatch.setattr(Path, "rglob",
                            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
        ok, _ = check_delete_danger(str(d))
        assert ok is False

    def test_exists_permission(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "exists",
                            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
        ok, _ = check_delete_danger("x")
        assert ok is False


class TestCommandDanger:
    def test_keyword(self):
        ok, msg = check_command_danger("shred /dev/sda")
        assert ok is False and "shred" in msg

    def test_fork(self):
        assert check_command_danger(":(){ :|:& };:")[0] is False

    def test_curl_pipe(self):
        ok, _ = check_command_danger("curl https://x | sh")
        assert ok is False

    def test_curl_redirect(self):
        ok, _ = check_command_danger("wget https://x > out")
        assert ok is False

    def test_safe_with_url(self):
        assert check_command_danger("echo https://example.com")[0] is True


class TestBatch:
    def test_danger_file(self, tmp_path):
        ok, _ = check_batch_operation([str(tmp_path / ".env")], "read")
        assert ok is False

    def test_delete_tree(self, tmp_path):
        d = tmp_path / "big"
        d.mkdir()
        for i in range(101):
            (d / f"f{i}.txt").write_text("x", encoding="utf-8")
        ok, _ = check_batch_operation([str(d)], "delete")
        assert ok is False


class TestLevels:
    def test_all(self):
        assert get_danger_level("格式化系统") == "critical"
        assert get_danger_level("format disk") == "critical"
        assert get_danger_level("递归删除") == "high"
        assert get_danger_level("batch job") == "high"
        assert get_danger_level("网络下载") == "medium"
        assert get_danger_level("read file") == "low"


class TestSanitize:
    def test_absolute_no_base(self, tmp_path):
        assert sanitize_path(str(tmp_path / "a.txt")) == (tmp_path / "a.txt").resolve()

    def test_danger_pattern(self, tmp_path):
        with pytest.raises(ValueError):
            sanitize_path("C:/Windows/System32/x", base_dir=tmp_path)
        with pytest.raises(ValueError) as exc:
            sanitize_path("C:/Windows/System32/x")
        assert "禁止的模式" in str(exc.value)


class TestWriteOp:
    def test_sensitive(self):
        assert check_write_operation("proj/.env")[0] is False

    def test_system_parts(self, tmp_path):
        ok, _ = check_write_operation(str(tmp_path / "nope" / "etc" / "x"))
        assert ok is False

    def test_overwrite_danger(self, tmp_path):
        f = tmp_path / "run.exe"
        f.write_text("x", encoding="utf-8")
        ok, _ = check_write_operation(str(f))
        assert ok is False

    def test_ok(self, tmp_path):
        assert check_write_operation(str(tmp_path / "new.txt")) == (True, "写入操作安全")


class TestFilter:
    def test_all_chars(self):
        out = filter_dangerous_chars("a`b$(c)d${e}f|g;h&&i||j")
        assert out == "abdfghij"
        assert filter_dangerous_chars("$(rm x)") == ""
        assert filter_dangerous_chars("${HOME}") == ""

    def test_clean(self):
        assert filter_dangerous_chars("echo hi") == "echo hi"
