"""MCP Hook 单一来源回归测试：上报只发生一次。

MCP 工具不经过 `_wrap_tool_with_hooks`（只有内置工具在 import 期被包），
其 hook 发射全在 `MCPRuntime._call_tool` 内。直接调用完整上报；
图内调用（``emit_events=False``）静默，由中间件统一上报，否则双发。
"""

from __future__ import annotations


class _StubClient:
    def __init__(self, result: str = "ok"):
        self._result = result

    def call_tool(self, name, arguments):
        return self._result


def _make_runtime(monkeypatch, tmp_path):
    import lib.core.mcp_runtime as mcp_mod
    from lib.core.mcp_runtime import MCPRuntime, MCPToolInfo

    events: list = []
    audits: list = []
    monkeypatch.setattr(mcp_mod, "trigger_hook_event", lambda *a, **k: events.append(a[0]) or None)
    monkeypatch.setattr(mcp_mod, "append_audit_event", lambda *a, **k: audits.append(a) or None)
    monkeypatch.setattr(mcp_mod, "enforce_tool_permission", lambda *a, **k: None)

    runtime = MCPRuntime()
    runtime.workspace = tmp_path
    runtime.tools_by_alias = {
        "mcp_fake_echo": MCPToolInfo(
            alias="mcp_fake_echo",
            server_name="fake",
            name="echo",
            description="fake echo",
            input_schema={},
        )
    }
    runtime.clients = {"fake": _StubClient()}
    return runtime, events, audits


def test_mcp_call_tool_fires_hooks_on_direct_call(monkeypatch, tmp_path):
    runtime, events, audits = _make_runtime(monkeypatch, tmp_path)
    assert runtime._call_tool("mcp_fake_echo", {"message": "hi"}) == "ok"
    assert "PreToolUse" in events
    assert "PostToolUse" in events
    assert len(audits) >= 1


def test_mcp_call_tool_stays_silent_for_middleware_call(monkeypatch, tmp_path):
    runtime, events, audits = _make_runtime(monkeypatch, tmp_path)
    assert runtime._call_tool("mcp_fake_echo", {"message": "hi"}, emit_events=False) == "ok"
    assert events == []
    assert audits == []
