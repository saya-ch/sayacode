# 文档一致性门禁：README 与架构图里的关键数字必须与实现一致。
# 加减工具时，先改实现，再同步文档，否则本文件会红。
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def test_builtin_tool_count():
    from lib.tools import _BUILTIN_TOOLS

    assert len(_BUILTIN_TOOLS) == 32


def test_deferred_builtin_count():
    from lib.core.tool_meta import get_tool_meta
    from lib.tools import _BUILTIN_TOOLS

    deferred = [
        tool.name
        for tool in _BUILTIN_TOOLS
        if (meta := get_tool_meta(str(tool.name))) and meta.should_defer and not meta.always_load
    ]
    assert len(deferred) == 10


def test_assembled_tool_counts():
    from lib.tools.registry import ToolRegistry

    context = SimpleNamespace(workspace=".", permissions=None, hooks=None, mode="build")
    names = [str(tool.name) for tool in ToolRegistry(context).build_tools()]
    plan_delegate = [name for name in names if name.startswith("plan_") or name.startswith("delegate_")]
    assert len(plan_delegate) == 9
    assert len(names) == 34


def test_readme_tool_numbers():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "44 个可用工具" in text
    assert "当前共 44 个" in text
    assert "只绑定 34 个工具" in text


def test_architecture_html_tool_numbers():
    text = (ROOT / "docs" / "architecture.html").read_text(encoding="utf-8")
    assert "32+3+3+6" in text
    assert "启动只绑 34 个" in text
