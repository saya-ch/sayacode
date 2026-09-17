"""中间件语义测试：政策进图后的判定必须与今天完全一致。

每条都对应今天的一处行为：
- allow/deny/ask 映射 = PermissionRuntime.check() 的三分支；
- grant_once 用后即焚，且覆盖不了 mode deny（今天弹窗从不覆盖）；
- peek 永不弹窗（今天 check() 会调 confirm_callback）；
- 拒绝消息以 ⚠️ 开头（agent._extract_tool_result 靠它归类"执行出错"）；
- 安全否决内容命中 _tool_result_was_blocked（hook 走 ToolFailure）；
- hook 顺序：Pre 先于执行、被拦走 ToolFailure("tool_blocked")、异常重抛。
"""

import pytest
from langchain_core.messages import ToolMessage

import lib.core.middleware as mw
from lib.core.middleware import (
    SayaHookMiddleware,
    SayaPermissionMiddleware,
    SayaPromptMiddleware,
    SayaSafetyMiddleware,
)
from lib.core.permissions import PermissionRuntime, SessionPermissionState
from lib.tools.context import ToolAbortController, set_abort_controller


@pytest.fixture(autouse=True)
def _fresh_abort_controller():
    """中止控制器是进程级 ContextVar 默认值：每个测试换新的，互不污染。"""
    set_abort_controller(ToolAbortController())
    yield
    set_abort_controller(ToolAbortController())


def _request(name="demo_tool", args=None, call_id="call-1"):
    from langchain.agents.middleware.types import ToolCallRequest

    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )


def _ok_handler(request):
    call = request.tool_call
    return ToolMessage(content="ok-result", name=call["name"], tool_call_id=call["id"])


def _isolated_runtime(**rules):
    runtime = PermissionRuntime(session=SessionPermissionState())
    if rules:
        runtime.update_session_rules(rules)
    return runtime


# ── 权限 ─────────────────────────────────────────────────────────────────────


def test_allow_passes_through_to_handler():
    middleware = SayaPermissionMiddleware(_isolated_runtime(demo_tool="allow"))

    result = middleware.wrap_tool_call(_request(), _ok_handler)

    assert isinstance(result, ToolMessage)
    assert result.content == "ok-result"


def test_deny_short_circuits_without_running_handler():
    calls = []
    middleware = SayaPermissionMiddleware(_isolated_runtime(demo_tool="deny"))

    result = middleware.wrap_tool_call(
        _request(), lambda request: calls.append(request) or _ok_handler(request)
    )

    assert calls == []
    assert isinstance(result, ToolMessage)
    assert str(result.content).startswith("⚠️")
    assert "Permission denied for tool" in str(result.content)


def test_deny_records_audit_like_check():
    runtime = _isolated_runtime(demo_tool="deny")
    middleware = SayaPermissionMiddleware(runtime)

    middleware.wrap_tool_call(_request(), _ok_handler)

    assert runtime.audit_log, "短路拒绝必须记审计——handler 没跑，工具体内联 check 也没跑"
    assert runtime.audit_log[-1]["allowed"] is False


def test_peek_never_prompts_even_with_callback_registered():
    def _boom(request):
        raise AssertionError("peek 绝不能弹窗")

    runtime = _isolated_runtime()
    runtime.set_confirm_callback(_boom)
    middleware = SayaPermissionMiddleware(runtime)

    # 默认策略是 ask：check() 到这里会弹窗，peek 必须只返回决策。
    decision = runtime.peek("demo_tool", {})

    assert decision.action == "ask"
    assert middleware is not None


def test_ask_interrupt_approve_runs_with_one_interrupt_per_grant(monkeypatch):
    """批准是"下一次判定消耗"：第 1 次中断→批准→执行，第 2 次直接放行，
    第 3 次再次中断。三次调用、两次中断，钉住"用后即焚"。"""
    interrupts = []

    def _fake_interrupt(payload):
        interrupts.append(payload)
        return {"approved": True}

    monkeypatch.setattr(mw, "interrupt", _fake_interrupt)
    middleware = SayaPermissionMiddleware(_isolated_runtime())

    assert middleware.wrap_tool_call(_request(), _ok_handler).content == "ok-result"
    assert middleware.wrap_tool_call(_request(), _ok_handler).content == "ok-result"
    assert middleware.wrap_tool_call(_request(), _ok_handler).content == "ok-result"

    assert len(interrupts) == 2
    assert interrupts[0]["tool"] == "demo_tool"
    assert "args_preview" in interrupts[0]


def test_ask_interrupt_deny_short_circuits(monkeypatch):
    monkeypatch.setattr(mw, "interrupt", lambda payload: {"approved": False})
    runtime = _isolated_runtime()
    middleware = SayaPermissionMiddleware(runtime)
    calls = []

    result = middleware.wrap_tool_call(
        _request(), lambda request: calls.append(request) or _ok_handler(request)
    )

    assert calls == []
    assert str(result.content).startswith("⚠️")
    assert runtime.audit_log[-1]["allowed"] is False


def test_grant_once_never_overrides_mode_deny(monkeypatch):
    """mode deny 是硬约束：今天弹窗根本不会弹，grant_once 也不能绕开。"""
    calls = []

    def _must_not_interrupt(payload):
        raise AssertionError("mode deny 不该走到中断")

    monkeypatch.setattr(mw, "interrupt", _must_not_interrupt)
    runtime = PermissionRuntime(session=SessionPermissionState())
    runtime.session.mode_rules = {"demo_tool": "deny"}
    runtime.grant_once("demo_tool")
    middleware = SayaPermissionMiddleware(runtime)

    result = middleware.wrap_tool_call(
        _request(), lambda request: calls.append(request) or _ok_handler(request)
    )

    assert calls == []
    assert str(result.content).startswith("⚠️")


def test_dangerous_floor_still_applies_to_rules_but_not_to_interrupt_grant():
    """规则里的 delete_file=allow 照样被地板拍成 deny；但用户中断里点的"仅本次"
    今天就是生效的（UI 明确写"仅本次生效"），grant_once 必须同样生效。"""
    runtime = PermissionRuntime(session=SessionPermissionState())
    runtime.session.session_rules = {"delete_file": "allow"}

    assert runtime.peek("delete_file", {}).action == "deny"

    runtime.grant_once("delete_file")
    assert runtime.peek("delete_file", {}).action == "allow"
    # 用后即焚：下一次回到地板。
    assert runtime.peek("delete_file", {}).action == "deny"


# ── 安全 ─────────────────────────────────────────────────────────────────────


def test_safety_denies_system_write_and_marks_blocked(tmp_path):
    middleware = SayaSafetyMiddleware()

    result = middleware.wrap_tool_call(
        _request("write_file", {"path": "C:\\Windows\\System32\\evil.txt"}),
        _ok_handler,
    )

    assert isinstance(result, ToolMessage)
    assert str(result.content).startswith("⚠️ 安全检查失败")

    from lib.tools import _tool_result_was_blocked

    assert _tool_result_was_blocked(str(result.content)), "必须走 ToolFailure 上报"


def test_safety_denies_dangerous_command():
    middleware = SayaSafetyMiddleware()

    result = middleware.wrap_tool_call(
        _request("execute_command_tool", {"command": "rm -rf / --no-preserve-root"}),
        _ok_handler,
    )

    assert str(result.content).startswith("⚠️")


def test_safety_passes_through_benign_calls(tmp_path):
    target = tmp_path / "notes.txt"
    middleware = SayaSafetyMiddleware()

    result = middleware.wrap_tool_call(
        _request("write_file", {"path": str(target)}), _ok_handler
    )

    assert result.content == "ok-result"


def test_safety_skips_tools_without_mappable_arguments():
    middleware = SayaSafetyMiddleware()

    result = middleware.wrap_tool_call(_request("list_directory", {"x": 1}), _ok_handler)

    assert result.content == "ok-result"


# ── Hook ─────────────────────────────────────────────────────────────────────


def _recorder():
    events = []

    def _trigger(event, payload=None):
        events.append((event, payload))
        return None

    return events, _trigger


def test_hook_pre_block_short_circuits():
    events, _ = _recorder()
    middleware = SayaHookMiddleware(
        trigger=lambda event, payload=None: (
            events.append((event, payload)) or ("nope" if event == "PreToolUse" else None)
        )
    )
    calls = []

    result = middleware.wrap_tool_call(
        _request(), lambda request: calls.append(request) or _ok_handler(request)
    )

    assert calls == []
    assert str(result.content).startswith("⚠️")
    assert [name for name, _ in events] == ["PreToolUse"]


def test_hook_post_fires_on_success():
    events, trigger = _recorder()
    middleware = SayaHookMiddleware(trigger=trigger)

    result = middleware.wrap_tool_call(_request(), _ok_handler)

    assert result.content == "ok-result"
    assert [name for name, _ in events] == ["PreToolUse", "PostToolUse"]


def test_hook_blocked_result_reports_tool_failure():
    events, trigger = _recorder()
    middleware = SayaHookMiddleware(trigger=trigger)

    def _denied(request):
        call = request.tool_call
        return ToolMessage(
            content="⚠️ Permission denied for tool 'demo_tool' by policy.",
            name=call["name"],
            tool_call_id=call["id"],
        )

    result = middleware.wrap_tool_call(_request(), _denied)

    kinds = [name for name, _ in events]
    assert kinds == ["PreToolUse", "ToolFailure"]
    failure = events[1][1]
    assert failure["error"] == "tool_blocked"
    assert "demo_tool" in str(result.content)


def test_hook_exception_reraises_and_aborts_siblings():
    from lib.tools.context import get_abort_controller

    events, trigger = _recorder()
    middleware = SayaHookMiddleware(trigger=trigger)

    def _boom(request):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        middleware.wrap_tool_call(_request("git_push", {}), _boom)

    assert [name for name, _ in events] == ["PreToolUse", "ToolFailure"]
    assert get_abort_controller().is_aborted


def test_hook_abort_short_circuits_before_pre():
    from lib.tools.context import get_abort_controller

    events, trigger = _recorder()
    middleware = SayaHookMiddleware(trigger=trigger)
    get_abort_controller().abort("sibling_error")
    result = middleware.wrap_tool_call(_request(), _ok_handler)

    assert "操作已中止" in str(result.content)
    assert events == [], "中止检查在 PreToolUse 之前"


# ── Prompt ───────────────────────────────────────────────────────────────────


def test_prompt_middleware_overrides_system_message():
    middleware = SayaPromptMiddleware()
    middleware.refresh("SYS-v1")
    seen = {}

    class _FakeRequest:
        def override(self, **kwargs):
            seen.update(kwargs)
            return self

    result = middleware.wrap_model_call(_FakeRequest(), lambda request: ("handled", request))

    assert result[0] == "handled"
    assert seen["system_message"].content == "SYS-v1"
