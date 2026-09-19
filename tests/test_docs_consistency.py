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
    from lib.agent.assembly import build_supervisor_factory
    from lib.tools.registry import ToolRegistry

    context = SimpleNamespace(workspace=".", permissions=None, hooks=None, mode="build")
    registry = ToolRegistry(context, supervisor_factory=build_supervisor_factory(context))
    names = [str(tool.name) for tool in registry.build_tools()]
    plan_delegate = [name for name in names if name.startswith("plan_") or name.startswith("delegate_")]
    assert len(plan_delegate) == 5
    assert len(names) == 30


def test_registry_without_factory_skips_delegate_tools():
    """不传 supervisor 工厂时跳过委托工具，核心与计划工具不受影响。"""
    from lib.tools.registry import ToolRegistry

    context = SimpleNamespace(workspace=".", permissions=None, hooks=None, mode="build")
    names = [str(tool.name) for tool in ToolRegistry(context).build_tools()]
    assert not [name for name in names if name.startswith("delegate_")]
    assert [name for name in names if name.startswith("plan_")]


def test_readme_tool_numbers():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "40 个可用工具" in text
    assert "当前共 40 个" in text
    assert "只绑定 30 个工具" in text
