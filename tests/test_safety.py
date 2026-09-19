from lib.core.safety_rules import (
    check_batch_operation,
    check_delete_danger,
    check_file_danger,
    sanitize_path,
)
from lib.core.context import ProjectContext


def _big_directory(tmp_path, count: int = 120):
    """造一个条目数超过删除阈值的目录。"""
    big = tmp_path / "big"
    big.mkdir()
    for index in range(count):
        (big / f"file_{index}.txt").write_text("x", encoding="utf-8")
    return big


def test_blocks_secret_key_files(tmp_path):
    secret_file = tmp_path / "id_rsa"
    secret_file.write_text("PRIVATE KEY", encoding="utf-8")

    is_safe, reason = check_file_danger(str(secret_file))

    assert not is_safe
    assert "敏感" in reason


def test_allows_env_example_templates(tmp_path):
    template_file = tmp_path / ".env.example"
    template_file.write_text("OPENAI_API_KEY=", encoding="utf-8")

    is_safe, reason = check_file_danger(str(template_file))

    assert is_safe, reason


def test_sanitize_path_blocks_sensitive_files_inside_workspace(tmp_path):
    try:
        sanitize_path("id_rsa", base_dir=tmp_path)
    except ValueError as exc:
        assert "敏感" in str(exc)
    else:
        raise AssertionError("sanitize_path should block sensitive files")


def test_sanitize_path_still_allows_normal_workspace_files(tmp_path):
    result = sanitize_path("README.md", base_dir=tmp_path)

    assert result == tmp_path.resolve() / "README.md"


def test_project_context_excludes_secret_files(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")

    context = ProjectContext(str(tmp_path))
    paths = {file_info.path for file_info in context.files}

    assert "main.py" in paths
    assert ".env" not in paths


# ── 删除专有判据不得污染只读操作 ──────────────────────────────────────────────
#
# 回归保护：这一项曾经长在 check_file_danger 里，而该函数只有一个 path 参数、没有
# 操作类型，于是所有调用方都继承了它 —— 结果只读的 list_directory 在超过 100 个
# 条目的目录上被拦下，并报出「批量删除存在风险」。真实仓库实测直接复现。
# 正确做法在 lib/core/safety.py 里本来就有：`if operation == 'delete':` 才做这项检查。


def test_file_danger_ignores_directory_size(tmp_path):
    """check_file_danger 只看路径本身，与操作类型无关。"""
    big = _big_directory(tmp_path)

    is_safe, reason = check_file_danger(str(big))

    assert is_safe, reason


def test_delete_danger_refuses_a_large_directory(tmp_path):
    """删除判据本身必须还在 —— 修 bug 不是把防护删掉。"""
    big = _big_directory(tmp_path)

    is_safe, reason = check_delete_danger(str(big))

    assert not is_safe
    assert "批量删除" in reason


def test_delete_danger_allows_a_small_directory(tmp_path):
    small = tmp_path / "small"
    small.mkdir()
    (small / "a.txt").write_text("x", encoding="utf-8")

    assert check_delete_danger(str(small))[0] is True


def test_delete_danger_allows_a_missing_path(tmp_path):
    assert check_delete_danger(str(tmp_path / "nope"))[0] is True


def test_delete_danger_allows_a_plain_file(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    assert check_delete_danger(str(target))[0] is True


def test_list_directory_works_on_a_large_directory(tmp_path):
    """只读的 list_directory 不得因为目录大而被拦下。"""
    from lib.tools.file_tools import list_directory, reset_workspace, use_workspace

    big = _big_directory(tmp_path)
    # list_directory 强制工作区边界；把工具工作区绑到 tmp_path 让「大树」落在允许范围内。
    token = use_workspace(tmp_path)
    try:
        output = str(list_directory.invoke({"path": str(big)}))
    finally:
        reset_workspace(token)

    assert "安全警告" not in output
    assert "file_0.txt" in output


def test_batch_delete_still_guards_a_large_directory(tmp_path):
    """批量删除路径必须继续拦住大树 —— 判据只是换了位置，不是消失。"""
    big = _big_directory(tmp_path)

    is_safe, reason = check_batch_operation([str(big)], "delete")

    assert not is_safe
    assert "批量删除" in reason


def test_batch_read_of_a_large_directory_is_not_guarded(tmp_path):
    """非删除的批量操作不受删除判据影响。"""
    big = _big_directory(tmp_path)

    is_safe, reason = check_batch_operation([str(big)], "modify")

    assert is_safe, reason
