# file_tools 全覆盖测试：读写、搜索、目录、批量原子编辑。

from pathlib import Path

import pytest

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


def _grant(tool_name, arguments=None):
    """测试前置批准：传本次调用的真实参数，只放行这一次。"""
    from lib.core.permission_session import _active_runtime

    _active_runtime().grant_once(tool_name, arguments)


@pytest.fixture
def ws(tmp_path):
    # 把文件工具工作区绑定到隔离目录的固件。
    token = use_workspace(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_workspace(token)


# ── 纯函数 ───────────────────────────────────────────────────────────────

class TestPureHelpers:
    def test_format_size_units(self):
        from lib.tools.file_tools import _format_size

        assert _format_size(0) == "0.0 B"
        assert _format_size(512) == "512.0 B"
        assert _format_size(2048) == "2.0 KB"
        assert _format_size(3 * 1024 * 1024) == "3.0 MB"
        assert _format_size(2 * 1024 ** 3) == "2.0 GB"
        assert _format_size(2 * 1024 ** 4) == "2.0 TB"

    def test_format_file_list_empty(self):
        from lib.tools.file_tools import _format_file_list

        assert _format_file_list([]) == "目录为空"

    def test_format_file_list_with_details(self, tmp_path):
        from lib.tools.file_tools import _format_file_list

        d = tmp_path / "sub"
        d.mkdir()
        f = tmp_path / "a.txt"
        f.write_text("hi", encoding="utf-8")
        out = _format_file_list([f, d])
        assert "[目录]" in out and "[文件]" in out
        assert "sub" in out and "a.txt" in out

    def test_format_file_list_no_details(self, tmp_path):
        from lib.tools.file_tools import _format_file_list

        f = tmp_path / "a.txt"
        f.write_text("hi", encoding="utf-8")
        out = _format_file_list([f], show_details=False)
        assert "a.txt" in out

    def test_read_with_encoding_fallback(self, tmp_path):
        from lib.tools.file_tools import _read_with_encoding

        f = tmp_path / "gbk.txt"
        f.write_bytes("中文".encode("gbk"))
        assert _read_with_encoding(f) == "中文"

    def test_read_with_encoding_all_fail(self, tmp_path):
        from lib.tools.file_tools import _read_with_encoding

        f = tmp_path / "bin.dat"
        f.write_bytes(b"\xff\xfe\x00\x01")
        assert _read_with_encoding(f, encodings=["ascii"]) is None
        assert _read_with_encoding(f, encodings=["no-such-codec"]) is None

    def test_validate_glob_pattern(self):
        from lib.tools.file_tools import _validate_glob_pattern

        assert _validate_glob_pattern("") is not None
        assert _validate_glob_pattern("   ") is not None
        assert _validate_glob_pattern("*.py") is None
        assert _validate_glob_pattern("/abs/*.py") is not None
        assert _validate_glob_pattern("C:/x/*.py") is not None
        assert _validate_glob_pattern("~/*.py") is not None
        assert _validate_glob_pattern("../*.py") is not None

    def test_safe_relative_match_outside_root(self, tmp_path):
        from lib.tools.file_tools import _safe_relative_match

        outside = Path.cwd().resolve()
        assert _safe_relative_match(outside, tmp_path) is None
        inside = tmp_path / "a.txt"
        inside.write_text("x", encoding="utf-8")
        assert _safe_relative_match(inside, tmp_path) is not None

    def test_normalize_file_type_filter(self):
        from lib.tools.file_tools import _normalize_file_type_filter

        ext, err = _normalize_file_type_filter("*.py")
        assert (ext, err) == ("py", None)
        ext, err = _normalize_file_type_filter("")
        assert ext is None and err
        ext, err = _normalize_file_type_filter("a/b")
        assert ext is None and err
        ext, err = _normalize_file_type_filter("..")
        assert ext is None and err
        ext, err = _normalize_file_type_filter("p@y")
        assert ext is None and err


# ── read_file ────────────────────────────────────────────────────────────

class TestReadFile:
    def test_read_ok(self, ws):
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        out = read_file.invoke({"path": "a.txt"})
        assert "hello" in out

    def test_read_missing(self, ws):
        out = read_file.invoke({"path": "nope.txt"})
        assert "不存在" in out

    def test_read_is_dir(self, ws):
        (ws / "sub").mkdir()
        out = read_file.invoke({"path": "sub"})
        assert "目录" in out

    def test_read_traversal_blocked(self, ws):
        out = read_file.invoke({"path": "../evil.txt"})
        assert "安全警告" in out

    def test_read_danger_path(self, ws):
        out = read_file.invoke({"path": "/etc/passwd"})
        assert "安全警告" in out or "不存在" in out

    def test_read_big_file_truncated(self, ws):
        big = "\n".join(f"line {i}" for i in range(3000))
        (ws / "big.txt").write_text(big + "x" * 60000, encoding="utf-8")
        out = read_file.invoke({"path": "big.txt"})
        assert "只显示前 100 行" in out
        assert "总行数" in out


# ── write_file ───────────────────────────────────────────────────────────

class TestWriteFile:
    def test_write_ok(self, ws):
        out = write_file.invoke({"path": "new.txt", "content": "abc"})
        assert "成功写入" in out
        assert (ws / "new.txt").read_text(encoding="utf-8") == "abc"

    def test_write_nested_dirs(self, ws):
        out = write_file.invoke({"path": "a/b/c.txt", "content": "deep"})
        assert "成功写入" in out
        assert (ws / "a" / "b" / "c.txt").exists()

    def test_write_traversal_blocked(self, ws):
        out = write_file.invoke({"path": "../evil.txt", "content": "x"})
        assert "安全警告" in out

    def test_write_sensitive_blocked(self, ws):
        out = write_file.invoke({"path": "id_rsa", "content": "x"})
        assert "安全警告" in out


# ── search_replace ───────────────────────────────────────────────────────

class TestSearchReplace:
    def test_replace_ok(self, ws):
        (ws / "a.txt").write_text("foo bar", encoding="utf-8")
        out = search_replace.invoke(
            {"file_path": "a.txt", "old_content": "foo", "new_content": "baz"}
        )
        assert "成功替换" in out
        assert (ws / "a.txt").read_text(encoding="utf-8") == "baz bar"
        (ws / "b.txt").write_text("foo bar foo", encoding="utf-8")
        out = search_replace.invoke(
            {"file_path": "b.txt", "old_content": "foo", "new_content": "baz"}
        )
        assert "不唯一" in out

    def test_replace_missing_file(self, ws):
        out = search_replace.invoke(
            {"file_path": "nope.txt", "old_content": "a", "new_content": "b"}
        )
        assert "不存在" in out

    def test_replace_not_found(self, ws):
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        out = search_replace.invoke(
            {"file_path": "a.txt", "old_content": "zzz", "new_content": "b"}
        )
        assert "未找到" in out

    def test_replace_traversal_blocked(self, ws):
        out = search_replace.invoke(
            {"file_path": "../evil.txt", "old_content": "a", "new_content": "b"}
        )
        assert "安全警告" in out


# ── glob_search ──────────────────────────────────────────────────────────

class TestGlobSearch:
    def test_glob_ok(self, ws):
        (ws / "a.py").write_text("x", encoding="utf-8")
        (ws / "b.txt").write_text("x", encoding="utf-8")
        out = glob_search.invoke({"pattern": "*.py"})
        assert "a.py" in out
        assert "b.txt" not in out

    def test_glob_no_match(self, ws):
        out = glob_search.invoke({"pattern": "*.zzz"})
        assert "没有找到" in out

    def test_glob_bad_pattern(self, ws):
        out = glob_search.invoke({"pattern": "../*.py"})
        assert "安全警告" in out

    def test_glob_missing_root(self, ws):
        out = glob_search.invoke({"pattern": "*.py", "root_dir": "no-such-dir"})
        assert "不存在" in out

    def test_glob_truncates_at_50(self, ws):
        for i in range(55):
            (ws / f"f{i:03d}.py").write_text("x", encoding="utf-8")
        out = glob_search.invoke({"pattern": "*.py"})
        assert "还有 5 个文件" in out


# ── grep_search ──────────────────────────────────────────────────────────

class TestGrepSearch:
    def test_grep_basic(self, ws):
        (ws / "a.py").write_text("hello world\nsecond\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "hello"})
        assert "a.py" in out
        assert "hello world" in out

    def test_grep_regex(self, ws):
        (ws / "a.py").write_text("abc123\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "abc\\d+", "regex": True})
        assert "abc123" in out

    def test_grep_case_sensitive(self, ws):
        (ws / "a.py").write_text("Hello\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "hello", "case_sensitive": True})
        assert "没有找到" in out
        out = grep_search.invoke({"pattern": "hello"})
        assert "Hello" in out

    def test_grep_file_type(self, ws):
        (ws / "a.py").write_text("needle\n", encoding="utf-8")
        (ws / "b.md").write_text("needle\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "needle", "file_type": "py"})
        assert "a.py" in out
        assert "b.md" not in out

    def test_grep_bad_file_type(self, ws):
        out = grep_search.invoke({"pattern": "x", "file_type": "a/b"})
        assert "安全警告" in out

    def test_grep_bad_regex(self, ws):
        (ws / "a.py").write_text("x\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "(unclosed", "regex": True})
        assert "正则表达式无效" in out

    def test_grep_no_match(self, ws):
        (ws / "a.py").write_text("hello\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "zzz-nope"})
        assert "没有找到" in out

    def test_grep_missing_root(self, ws):
        out = grep_search.invoke({"pattern": "x", "root_dir": "no-such-dir"})
        assert "不存在" in out

    def test_grep_max_results(self, ws):
        (ws / "a.py").write_text("\n".join(f"hit {i}" for i in range(20)), encoding="utf-8")
        out = grep_search.invoke({"pattern": "hit", "max_results": 5})
        assert "最大返回条数 5" in out

    def test_grep_long_line_truncated(self, ws):
        (ws / "a.py").write_text("x" * 200 + "\n", encoding="utf-8")
        out = grep_search.invoke({"pattern": "x+" , "regex": True})
        assert "..." in out


# ── 目录工具 ─────────────────────────────────────────────────────────────

class TestDirectories:
    def test_create_ok(self, ws):
        out = create_directory.invoke({"path": "a/b"})
        assert "成功创建" in out
        assert (ws / "a" / "b").is_dir()

    def test_create_exists(self, ws):
        (ws / "a").mkdir()
        out = create_directory.invoke({"path": "a"})
        assert "已存在" in out

    def test_create_traversal_blocked(self, ws):
        out = create_directory.invoke({"path": "../evil"})
        assert "安全警告" in out

    def test_delete_blocked_by_default_policy(self, ws):
        (ws / "a.txt").write_text("x", encoding="utf-8")
        out = delete_file.invoke({"path": "a.txt"})
        assert "ermission" in out
        assert (ws / "a.txt").exists()

    def test_delete_file_ok(self, ws):
        (ws / "a.txt").write_text("x", encoding="utf-8")
        _grant("delete_file", {"path": "a.txt"})
        out = delete_file.invoke({"path": "a.txt"})
        assert "已删除" in out
        assert not (ws / "a.txt").exists()

    def test_delete_empty_dir(self, ws):
        (ws / "d").mkdir()
        _grant("delete_file", {"path": "d"})
        out = delete_file.invoke({"path": "d"})
        assert "已删除" in out

    def test_delete_nonempty_dir_refused(self, ws):
        (ws / "d").mkdir()
        (ws / "d" / "a.txt").write_text("x", encoding="utf-8")
        _grant("delete_file", {"path": "d"})
        out = delete_file.invoke({"path": "d"})
        assert "不为空" in out
        assert (ws / "d").exists()

    def test_delete_missing(self, ws):
        _grant("delete_file", {"path": "nope.txt"})
        out = delete_file.invoke({"path": "nope.txt"})
        assert "不存在" in out

    def test_delete_traversal_blocked(self, ws):
        _grant("delete_file", {"path": "../evil.txt"})
        out = delete_file.invoke({"path": "../evil.txt"})
        assert "安全警告" in out or "危险操作" in out

    def test_list_ok(self, ws):
        (ws / "sub").mkdir()
        (ws / "a.txt").write_text("hi", encoding="utf-8")
        out = list_directory.invoke({"path": "."})
        assert "sub" in out and "a.txt" in out
        assert "子目录" in out and "文件" in out

    def test_list_empty(self, ws):
        (ws / "empty").mkdir()
        out = list_directory.invoke({"path": "empty"})
        assert "为空" in out

    def test_list_missing(self, ws):
        out = list_directory.invoke({"path": "nope"})
        assert "不存在" in out

    def test_list_not_a_dir(self, ws):
        (ws / "a.txt").write_text("x", encoding="utf-8")
        out = list_directory.invoke({"path": "a.txt"})
        assert "不是目录" in out

    def test_list_truncates_files(self, ws):
        for i in range(25):
            (ws / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
        out = list_directory.invoke({"path": "."})
        assert "还有 5 个文件" in out


# ── batch_edit ───────────────────────────────────────────────────────────

class TestBatchEdit:
    def test_empty(self, ws):
        assert "未提供" in batch_edit.invoke({"edits": []})

    def test_write_new_files(self, ws):
        out = batch_edit.invoke(
            {"edits": [
                {"path": "a.txt", "operation": "write", "content": "aaa"},
                {"path": "b.txt", "operation": "write", "content": "bbb"},
            ]}
        )
        assert "批量编辑完成: 2 个操作" in out
        assert (ws / "a.txt").read_text(encoding="utf-8") == "aaa"

    def test_replace_ok(self, ws):
        (ws / "a.txt").write_text("foo bar", encoding="utf-8")
        out = batch_edit.invoke(
            {"edits": [
                {"path": "a.txt", "operation": "replace",
                 "old_content": "foo", "new_content": "baz"},
            ]}
        )
        assert "批量编辑完成" in out
        assert "替换" in out
        assert (ws / "a.txt").read_text(encoding="utf-8") == "baz bar"

    def test_non_dict_item_rejected_by_schema(self, ws):
        import pytest as _pytest
        from pydantic import ValidationError as _ValidationError

        with _pytest.raises(_ValidationError):
            batch_edit.invoke({"edits": ["oops"]})

    def test_non_dict_item_branch(self, ws):
        out = batch_edit.func(["oops"])
        assert "验证失败" in out
        assert "必须是字典" in out

    def test_missing_path_or_operation(self, ws):
        out = batch_edit.invoke({"edits": [{"path": "a.txt"}]})
        assert "缺少 path 或 operation" in out

    def test_bad_operation(self, ws):
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "delete"}]}
        )
        assert "不支持的操作类型" in out

    def test_bad_path(self, ws):
        out = batch_edit.invoke(
            {"edits": [{"path": "../evil.txt", "operation": "write", "content": "x"}]}
        )
        assert "验证失败" in out

    def test_replace_missing_file(self, ws):
        out = batch_edit.invoke(
            {"edits": [{"path": "nope.txt", "operation": "replace",
                        "old_content": "a", "new_content": "b"}]}
        )
        assert "需要目标文件" in out

    def test_replace_missing_old_content(self, ws):
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "replace", "new_content": "b"}]}
        )
        assert "缺少 old_content" in out

    def test_replace_old_not_found(self, ws):
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "replace",
                        "old_content": "zzz", "new_content": "b"}]}
        )
        assert "未找到匹配" in out

    def test_atomicity_on_validation_error(self, ws):
        (ws / "keep.txt").write_text("orig", encoding="utf-8")
        out = batch_edit.invoke(
            {"edits": [
                {"path": "keep.txt", "operation": "replace",
                 "old_content": "orig", "new_content": "changed"},
                {"path": "bad.txt", "operation": "delete"},
            ]}
        )
        assert "验证失败" in out
        assert (ws / "keep.txt").read_text(encoding="utf-8") == "orig"

    def test_rollback_on_ambiguous_replace(self, ws):
        (ws / "a.txt").write_text("dup dup", encoding="utf-8")
        out = batch_edit.invoke(
            {"edits": [
                {"path": "a.txt", "operation": "write", "content": "new"},
                {"path": "b.txt", "operation": "replace",
                 "old_content": "x", "new_content": "y"},
            ]}
        )
        # b.txt 不存在 → 验证失败，a.txt 不应被改
        assert "验证失败" in out
        assert (ws / "a.txt").read_text(encoding="utf-8") == "dup dup"

    def test_write_safety_check_failure(self, ws):
        out = batch_edit.invoke(
            {"edits": [{"path": "id_rsa", "operation": "write", "content": "x"}]}
        )
        assert "验证失败" in out

    def test_diff_truncated(self, ws):
        big_old = "\n".join(f"old {i}" for i in range(200))
        big_new = "\n".join(f"new {i}" for i in range(200))
        (ws / "a.txt").write_text(big_old, encoding="utf-8")
        out = batch_edit.invoke(
            {"edits": [{"path": "a.txt", "operation": "write", "content": big_new}]}
        )
        assert "批量编辑完成" in out
        assert "截断" in out

    def test_workspace_helpers(self, ws, tmp_path):
        assert get_default_workspace() == tmp_path.resolve()
