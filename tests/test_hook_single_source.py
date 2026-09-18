"""Hook 单一来源回归测试：wrapper 只执行，发射全归中间件。

``SayaHookMiddleware`` 是 Pre/Post/审计的唯一发射方，
工具体内 ``_wrap_tool_with_hooks`` 只做执行 + 中止检查。
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool


def _make_wrapped():
    from lib.tools import _wrap_tool_with_hooks

    def _impl(x: str = "") -> str:
        """测试探针工具。"""
        return f"ok:{x}"

    tool = StructuredTool.from_function(
        func=_impl, name="hook_single_source_probe", description="测试探针工具"
    )
    return _wrap_tool_with_hooks(tool)


def test_wrapper_never_emits_hooks(monkeypatch):
    from lib.core import hooks as hooks_mod

    events: list = []
    monkeypatch.setattr(
        hooks_mod, "trigger_hook_event", lambda *a, **k: events.append(a[0]) or None
    )

    wrapped = _make_wrapped()
    assert wrapped.func(x="b") == "ok:b"
    assert events == []
