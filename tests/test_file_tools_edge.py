# file_tools 异常分支与回滚链：全部用 monkeypatch 定点触发。

from pathlib import Path

import pytest

import lib.tools.file_tools as ft
from lib.tools.file_tools import (
    batch_edit,
    create_directory,
    delete_file,
    get_default_workspace,
    glob_search,
    grep_search,
    list_directory,
    read_file,
    reset_workspace,
    search_replace,
    use_workspace,
    write_file,
)


@pytest.fixture
def ws(tmp_path):
    # 隔离工作区固件。
    token = use_workspace(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_workspace(token)


def _deny_all(monkeypatch):
    # 让权限检查一律拒绝，覆盖各工具的 permission_error 分支。
    monkeypatch.setattr(ft, "enforce_tool_permission", lambda *a, **k: "denied")


class TestPermissionDenied:
    def test_write_denied(self, ws, monkeypatch):
        _deny_all(monkeypatch)
        assert write_file.invoke({"path": "a.txt", "content": "x"}) == "denied"

    def test_replace_denied(self, ws, monkeypatch):
        _deny_all(monkeypatch)
        out = search_replace.invoke(
            {"file_path": "a.txt", "old_content": "a", "new_content": "b"}
        )
        assert out == "denied"

    def test_mkdir_denied(self, ws, monkeypatch):
        _deny_all(monkeypatch)
        assert create_directory.invoke({"path": "d"}) == "denied"

    def test_batch_denied(self, ws, monkeypatch):
        _deny_all(monkeypatch)
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "write", "content": "x"}]}
        )
        assert out == "denied"


class TestDangerBranches:
    def test_read_danger(self, ws, monkeypatch):
        (ws / "a.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(ft, "check_file_danger", lambda p: (False, "mock-danger"))
        out = read_file.invoke({"path": "a.txt"})
        assert "mock-danger" in out

    def test_write_overwrite_danger(self, ws, monkeypatch):
        (ws / "a.txt").write_text("orig", encoding="utf-8")
        monkeypatch.setattr(ft, "check_write_operation", lambda p: (True, ""))
        monkeypatch.setattr(ft, "check_file_danger", lambda p: (False, "protected"))
        out = write_file.invoke({"path": "a.txt", "content": "new"})
        assert "protected" in out
        assert (ws / "a.txt").read_text(encoding="utf-8") == "orig"

    def test_write_check_fail(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "check_write_operation", lambda p: (False, "no-write"))
        out = write_file.invoke({"path": "a.txt", "content": "x"})
        assert "no-write" in out

    def test_replace_danger(self, ws, monkeypatch):
        (ws / "a.txt").write_text("aaa", encoding="utf-8")
        monkeypatch.setattr(ft, "check_file_danger", lambda p: (False, "mock-danger"))
        out = search_replace.invoke(
            {"file_path": "a.txt", "old_content": "a", "new_content": "b"}
        )
        assert "mock-danger" in out

    def test_mkdir_danger(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "check_file_danger", lambda p: (False, "mock-danger"))
        out = create_directory.invoke({"path": "d"})
        assert "mock-danger" in out

    def test_delete_danger(self, ws, monkeypatch):
        from lib.core.permissions import _active_runtime

        (ws / "a.txt").write_text("x", encoding="utf-8")
        _active_runtime().grant_once("delete_file")
        monkeypatch.setattr(ft, "check_file_danger", lambda p: (False, "mock-danger"))
        out = delete_file.invoke({"path": "a.txt"})
        assert "mock-danger" in out
        assert (ws / "a.txt").exists()

    def test_list_danger(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "check_file_danger", lambda p: (False, "mock-danger"))
        out = list_directory.invoke({"path": "."})
        assert "mock-danger" in out


class TestUnreadableContent:
    def test_read_none_content(self, ws, monkeypatch):
        (ws / "a.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(ft, "_read_with_encoding", lambda p: None)
        out = read_file.invoke({"path": "a.txt"})
        assert "编码不支持" in out

    def test_replace_none_content(self, ws, monkeypatch):
        (ws / "a.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(ft, "_read_with_encoding", lambda p: None)
        out = search_replace.invoke(
            {"file_path": "a.txt", "old_content": "a", "new_content": "b"}
        )
        assert "无法读取" in out


class TestGenericErrors:
    def test_read_resolve_crash(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = read_file.invoke({"path": "a.txt"})
        assert "读取文件出错" in out

    def test_write_crash(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = write_file.invoke({"path": "a.txt", "content": "x"})
        assert "写入文件出错" in out

    def test_replace_crash(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = search_replace.invoke(
            {"file_path": "a.txt", "old_content": "a", "new_content": "b"}
        )
        assert "替换操作出错" in out

    def test_glob_crash(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = glob_search.invoke({"pattern": "*.py"})
        assert "glob" in out and "boom" in out

    def test_grep_crash(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = grep_search.invoke({"pattern": "x"})
        assert "grep" in out and "boom" in out

    def test_mkdir_crash(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = create_directory.invoke({"path": "d"})
        assert "创建目录出错" in out

    def test_delete_crash(self, ws, monkeypatch):
        from lib.core.permissions import _active_runtime

        _active_runtime().grant_once("delete_file")
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = delete_file.invoke({"path": "a.txt"})
        assert "删除操作出错" in out

    def test_list_crash(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "_safe_resolve_path", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        out = list_directory.invoke({"path": "."})
        assert "列出目录出错" in out


class TestListFallbacks:
    def _fail_stat_for(self, monkeypatch, *names):
        import errno as _errno
        import os as _os

        def fake_stat(self, follow_symlinks=True):
            if Path(self).name in names:
                raise OSError(_errno.ENOENT, "gone")
            return _os.stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", fake_stat)

    def test_format_list_stat_fails(self, tmp_path, monkeypatch):
        from lib.tools.file_tools import _format_file_list

        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        self._fail_stat_for(monkeypatch, "a.txt")
        out = _format_file_list([f])
        assert "a.txt" in out

    def test_list_subdir_count_fails(self, ws):
        from unittest import mock

        (ws / "sub").mkdir()
        (ws / "sub" / "x.txt").write_text("x", encoding="utf-8")
        real_iterdir = Path.iterdir

        def flaky(self):
            if self.name == "sub":
                raise OSError("busy")
            return real_iterdir(self)

        with mock.patch.object(Path, "iterdir", flaky):
            out = list_directory.invoke({"path": "."})
        assert "sub" in out

    def test_list_file_size_fails(self, ws, monkeypatch):
        import errno as _errno
        import os as _os

        (ws / "a.txt").write_text("x", encoding="utf-8")
        calls = {"n": 0}

        def flaky_stat(self, follow_symlinks=True):
            # is_file 的第一次 stat 放行，取大小的第二次 stat 炸掉。
            if Path(self).name == "a.txt":
                calls["n"] += 1
                if calls["n"] >= 3:
                    raise OSError(_errno.ENOENT, "gone")
            return _os.stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        out = list_directory.invoke({"path": "."})
        assert "a.txt" in out

    def test_delete_iterdir_fails_then_rmdir_fails(self, ws):
        from unittest import mock
        from lib.core.permissions import _active_runtime

        (ws / "d").mkdir()
        (ws / "d" / "a.txt").write_text("x", encoding="utf-8")
        _active_runtime().grant_once("delete_file")
        with mock.patch.object(Path, "iterdir", side_effect=OSError("busy")):
            out = delete_file.invoke({"path": "d"})
        assert "删除操作出错" in out
        assert (ws / "d").exists()


class TestGlobGrepEdges:
    def test_glob_dedup(self, ws, monkeypatch):
        from unittest import mock

        (ws / "a.py").write_text("x", encoding="utf-8")
        real_glob = Path.glob

        def double(self, pattern):
            found = list(real_glob(self, pattern))
            return found + found

        with mock.patch.object(Path, "glob", double):
            out = glob_search.invoke({"pattern": "*.py"})
        assert "a.py" in out
        assert "找到 1 个" in out

    def test_grep_skips_directory_match(self, ws):
        (ws / "odd.py").mkdir()
        (ws / "a.py").write_text("hello\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "hello"})
        assert "a.py" in out

    def test_grep_file_read_error_skipped(self, ws, monkeypatch):
        (ws / "a.py").write_text("needle\n", encoding="utf-8")
        (ws / "b.py").write_text("needle\n", encoding="utf-8")
        real_reader = ft._read_with_encoding

        def flaky(path):
            if path.name == "a.py":
                raise RuntimeError("busy")
            return real_reader(path)

        monkeypatch.setattr(ft, "_read_with_encoding", flaky)
        out = grep_search.invoke({"pattern": "needle"})
        assert "b.py" in out


class TestValueErrors:
    def test_glob_root_traversal(self, ws):
        out = glob_search.invoke({"pattern": "*.py", "root_dir": "../evil"})
        assert "安全警告" in out

    def test_grep_root_traversal(self, ws):
        out = grep_search.invoke({"pattern": "x", "root_dir": "../evil"})
        assert "安全警告" in out

    def test_list_root_traversal(self, ws):
        out = list_directory.invoke({"path": "../evil"})
        assert "安全警告" in out

    def test_set_default_workspace_roundtrip(self, tmp_path):
        from lib.tools.file_tools import set_default_workspace

        set_default_workspace(tmp_path)
        assert get_default_workspace() == tmp_path.resolve()
        set_default_workspace(Path.cwd())

    def test_grep_none_content_skipped(self, ws, monkeypatch):
        (ws / "a.py").write_text("needle\n", encoding="utf-8")
        (ws / "b.py").write_text("needle\n", encoding="utf-8")
        real_reader = ft._read_with_encoding

        def none_for_a(path):
            if path.name == "a.py":
                return None
            return real_reader(path)

        monkeypatch.setattr(ft, "_read_with_encoding", none_for_a)
        out = grep_search.invoke({"pattern": "needle"})
        assert "b.py" in out


class TestBatchRollbackFailures:
    def test_rollback_write_fails_reported(self, ws, monkeypatch):
        (ws / "a.txt").write_text("solo", encoding="utf-8")
        real_text = Path.read_text
        real_write = Path.write_text
        calls = {"n": 0}

        def vanishing_read(self, *args, **kwargs):
            if self.name == "a.txt":
                calls["n"] += 1
                if calls["n"] >= 2:
                    return "nothing here"
            return real_text(self, *args, **kwargs)

        def fail_rollback(self, data, *args, **kwargs):
            if self.name == "a.txt" and data == "solo":
                raise RuntimeError("disk gone")
            return real_write(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", vanishing_read)
        monkeypatch.setattr(Path, "write_text", fail_rollback)
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "replace",
                        "old_content": "solo", "new_content": "CHANGED"}]}
        )
        assert "回滚失败" in out

    def test_rollback_cleanup_failure_silent(self, ws, monkeypatch):
        (ws / "a.txt").write_text("solo", encoding="utf-8")
        real_text = Path.read_text
        calls = {"n": 0}

        def vanishing_read(self, *args, **kwargs):
            if self.name == "a.txt":
                calls["n"] += 1
                if calls["n"] >= 2:
                    return "nothing here"
            return real_text(self, *args, **kwargs)

        def fail_unlink(self, *args, **kwargs):
            raise RuntimeError("locked")

        monkeypatch.setattr(Path, "read_text", vanishing_read)
        monkeypatch.setattr(Path, "unlink", fail_unlink)
        out = batch_edit.invoke(
            {"edits": [
                {"path": "fresh.txt", "operation": "write", "content": "new"},
                {"path": "a.txt", "operation": "replace",
                 "old_content": "solo", "new_content": "CHANGED"},
            ]}
        )
        assert "已回滚" in out
        assert (ws / "fresh.txt").exists()


class TestBatchEdgePaths:
    def test_batch_write_safety_fail(self, ws, monkeypatch):
        monkeypatch.setattr(ft, "check_write_operation", lambda p: (False, "no-write"))
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "write", "content": "x"}]}
        )
        assert "写入安全检查失败" in out

    def test_batch_replace_danger(self, ws, monkeypatch):
        (ws / "a.txt").write_text("aaa", encoding="utf-8")
        monkeypatch.setattr(ft, "check_file_danger", lambda p: (False, "mock-danger"))
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "replace",
                        "old_content": "a", "new_content": "b"}]}
        )
        assert "文件安全检查失败" in out

    def test_batch_safety_crash(self, ws, monkeypatch):
        (ws / "a.txt").write_text("aaa", encoding="utf-8")

        def boom(path):
            raise RuntimeError("scanner down")

        monkeypatch.setattr(ft, "check_file_danger", boom)
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "replace",
                        "old_content": "a", "new_content": "b"}]}
        )
        assert "安全检查异常" in out

    def test_batch_read_original_fails(self, ws):
        (ws / "sub").mkdir()
        out = batch_edit.invoke(
            {"edits": [{"path": "sub", "operation": "write", "content": "x"}]}
        )
        assert "读取原文件失败" in out

    def test_batch_exec_conflict_triggers_rollback(self, ws, monkeypatch):
        # 验证期读到单次匹配，执行期重读变成多次：触发执行失败与回滚。
        (ws / "a.txt").write_text("solo", encoding="utf-8")
        real_text = Path.read_text
        calls = {"n": 0}

        def flaky_read(self, *args, **kwargs):
            if self.name == "a.txt":
                calls["n"] += 1
                if calls["n"] >= 2:
                    return "solo solo"
            return real_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky_read)
        out = batch_edit.invoke(
            {"edits": [
                {"path": "a.txt", "operation": "replace",
                 "old_content": "solo", "new_content": "CHANGED"},
                {"path": "fresh.txt", "operation": "write", "content": "new"},
            ]}
        )
        assert "已回滚 1 个文件" in out
        assert "不唯一" in out
        assert "已清理新建文件" in out
        # 断言必须绕过上面的 read_text 补丁读盘，防止计数器污染。
        assert real_text(ws / "a.txt", encoding="utf-8") == "solo"
        assert not (ws / "fresh.txt").exists()

    def test_batch_exec_conflict_zero_match_rolls_back(self, ws, monkeypatch):
        # 执行期重读发现匹配消失：同样回滚。
        (ws / "a.txt").write_text("solo", encoding="utf-8")
        real_text = Path.read_text
        calls = {"n": 0}

        def vanishing_read(self, *args, **kwargs):
            if self.name == "a.txt":
                calls["n"] += 1
                if calls["n"] >= 2:
                    return "nothing here"
            return real_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", vanishing_read)
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "replace",
                        "old_content": "solo", "new_content": "CHANGED"}]}
        )
        assert "并发修改" in out
        assert real_text(ws / "a.txt", encoding="utf-8") == "solo"
