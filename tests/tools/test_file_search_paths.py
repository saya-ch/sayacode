"""回归：文件检索使用工作区内真实路径，且真实绝对路径参数生效。

真实事故：ansible 任务中 `glob_search(pattern="**/module_utils/network/icx/icx.py")`
返回 `/lib/ansible/...`（丢了工作区 `/app` 前缀），而
`glob_search(pattern="test/units/...", path="/app")` 返回 `No files found`，
模型因此认为工作树干净、找不到测试文件，最终产出 0B patch。
"""

from __future__ import annotations

from pathlib import Path

from sayacode.tools.search import WorkspaceFileSearchMiddleware


def _workspace(tmp_path: Path) -> WorkspaceFileSearchMiddleware:
    (tmp_path / "lib/ansible/module_utils/network/icx").mkdir(parents=True)
    (tmp_path / "lib/ansible/module_utils/network/icx/icx.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (tmp_path / "test/units/modules/network/icx").mkdir(parents=True)
    (tmp_path / "test/units/modules/network/icx/test_icx_command.py").write_text(
        "MATCH_TOKEN = 1\n", encoding="utf-8"
    )
    return WorkspaceFileSearchMiddleware(root_path=str(tmp_path))


def test_relative_pattern_returns_workspace_absolute_path(tmp_path: Path) -> None:
    search = _workspace(tmp_path)
    result = search.glob_search.invoke({"pattern": "**/module_utils/network/icx/icx.py"})
    expected = (tmp_path / "lib/ansible/module_utils/network/icx/icx.py").resolve()
    assert result == str(expected)
    assert result.startswith(str(tmp_path.resolve()))
    # 返回的路径必须能直接被文件工具打开。
    assert Path(result).is_file()


def test_explicit_workspace_absolute_path_argument_is_honoured(tmp_path: Path) -> None:
    """容器里工作区是 /app，模型显式传 /app；这里用真实工作区绝对路径等价复现。"""
    search = _workspace(tmp_path)
    result = search.glob_search.invoke(
        {
            "pattern": "test/units/modules/network/icx/test_icx_*.py",
            "path": str(tmp_path),
        }
    )
    expected = (tmp_path / "test/units/modules/network/icx/test_icx_command.py").resolve()
    assert result == str(expected)


def test_virtual_root_still_selects_workspace_root(tmp_path: Path) -> None:
    search = _workspace(tmp_path)
    # 尾段 `**` 只在 Python 3.13 起同时返回文件和目录；3.11/3.12 仅返回目录，
    # 会被官方实现的 is_file 过滤成 No files found。匹配文件须显式给出文件段。
    result = search.glob_search.invoke(
        {"pattern": "**/network/icx/**/*.py", "path": "/"}
    )
    lines = result.splitlines()
    assert len(lines) == 2
    assert all(Path(line).is_file() for line in lines)
    assert all(line.startswith(str(tmp_path.resolve())) for line in lines)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    search = _workspace(tmp_path)
    assert search.glob_search.invoke({"pattern": "**/*.py", "path": "../.."}) == "No files found"


def test_grep_returns_workspace_absolute_paths(tmp_path: Path) -> None:
    search = _workspace(tmp_path)
    content = search.grep_search.invoke(
        {"pattern": "MATCH_TOKEN", "output_mode": "content"}
    )
    expected = (tmp_path / "test/units/modules/network/icx/test_icx_command.py").resolve()
    assert content == f"{expected}:1:MATCH_TOKEN = 1"


def test_grep_accepts_real_absolute_path_argument(tmp_path: Path) -> None:
    search = _workspace(tmp_path)
    paths = search.grep_search.invoke({"pattern": "MATCH_TOKEN", "path": str(tmp_path)})
    expected = (tmp_path / "test/units/modules/network/icx/test_icx_command.py").resolve()
    assert paths == str(expected)
