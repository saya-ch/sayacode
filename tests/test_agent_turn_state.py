"""P0: Agent 循环鲁棒性测试 — TurnTransition, TurnState, ToolAbortController."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from lib.agent import SAIAgent
from lib.core.agent_runtime import TurnTransition, TurnState
from lib.core.session import SessionManager
from lib.tools.context import ToolAbortController, get_abort_controller


class TestTurnTransition:
    def test_all_transitions_exist(self):
        assert TurnTransition.NEXT_TURN.value == "next_turn"
        assert TurnTransition.COMPLETED.value == "completed"
        assert TurnTransition.STREAM_INTERRUPTED.value == "stream_interrupted"
        assert TurnTransition.MODEL_ERROR.value == "model_error"
        assert TurnTransition.MAX_RETRIES.value == "max_retries"
        assert TurnTransition.ABORTED.value == "aborted"

    def test_transition_uniqueness(self):
        values = [t.value for t in TurnTransition]
        assert len(values) == len(set(values))


class TestTurnState:
    def test_default_state(self):
        ts = TurnState()
        assert ts.transition == TurnTransition.COMPLETED
        assert ts.turn_count == 0
        assert ts.tool_use_count == 0
        assert ts.needs_follow_up is False
        assert ts.error_message == ""

    def test_is_terminal(self):
        terminal_states = [
            TurnTransition.COMPLETED,
            TurnTransition.MODEL_ERROR,
            TurnTransition.MAX_RETRIES,
            TurnTransition.ABORTED,
        ]
        for t in terminal_states:
            ts = TurnState(transition=t)
            assert ts.is_terminal, f"{t} should be terminal"

    def test_not_terminal(self):
        non_terminal = [
            TurnTransition.NEXT_TURN,
            TurnTransition.STREAM_INTERRUPTED,
        ]
        for t in non_terminal:
            ts = TurnState(transition=t)
            assert not ts.is_terminal, f"{t} should NOT be terminal"

    def test_should_continue(self):
        ts = TurnState(transition=TurnTransition.NEXT_TURN, needs_follow_up=True)
        assert ts.should_continue

    def test_should_not_continue_without_follow_up(self):
        ts = TurnState(transition=TurnTransition.NEXT_TURN, needs_follow_up=False)
        assert not ts.should_continue

    def test_should_not_continue_when_completed(self):
        ts = TurnState(transition=TurnTransition.COMPLETED, needs_follow_up=True)
        assert not ts.should_continue

    def test_turn_count_tracking(self):
        ts = TurnState(turn_count=5, tool_use_count=3)
        assert ts.turn_count == 5
        assert ts.tool_use_count == 3

    def test_error_message_tracking(self):
        ts = TurnState(
            transition=TurnTransition.MODEL_ERROR,
            error_message="Connection timeout"
        )
        assert ts.error_message == "Connection timeout"
        assert ts.is_terminal


class TestToolAbortController:
    def test_initial_state(self):
        ac = ToolAbortController()
        assert not ac.is_aborted
        assert ac.reason == "unknown"  # 默认值

    def test_abort_sets_state(self):
        ac = ToolAbortController()
        ac.abort("sibling_error")
        assert ac.is_aborted
        assert ac.reason == "sibling_error"

    def test_abort_preserves_reason(self):
        ac = ToolAbortController()
        ac.abort("bash_failed")
        assert ac.reason == "bash_failed"

    def test_reset(self):
        ac = ToolAbortController()
        ac.abort("error")
        ac.reset()
        assert not ac.is_aborted
        assert ac.reason == "unknown"

    def test_multiple_aborts(self):
        ac = ToolAbortController()
        ac.abort("first")
        ac.abort("second")
        assert ac.reason == "second"

    def test_sibling_error_pattern(self):
        """模拟 sibling abort 模式：bash 工具失败后，同级工具检查并返回中止信息。"""
        ac = ToolAbortController()
        # 模拟 bash 失败
        ac.abort("sibling_error")

        # 同级 git 工具检查
        if ac.is_aborted:
            result = "⚠️ 操作已中止: " + ac.reason
        assert "中止" in result
        assert "sibling_error" in result

    def test_get_abort_controller_default(self):
        ac = get_abort_controller()
        assert isinstance(ac, ToolAbortController)
        assert not ac.is_aborted


def test_prompt_builder_restores_assistant_provider_metadata(tmp_path):
    session = SessionManager()
    session.add_user_message("previous")
    session.add_assistant_message(
        "answer",
        metadata={"additional_kwargs": {"reasoning_content": "opaque"}},
    )
    session.add_user_message("next")
    from lib.agent.assembly import history_messages

    messages = history_messages(session)

    assistant = next(message for message in messages if isinstance(message, AIMessage))
    assert assistant.additional_kwargs == {"reasoning_content": "opaque"}


def _bare_agent(tmp_path):
    agent = object.__new__(SAIAgent)
    agent._turn_count = 0
    agent._abort_controller = ToolAbortController()
    agent._last_extra = {}
    agent._recovery_state = {}
    agent._permissions_runtime = None
    agent._hooks_runtime = None
    agent.workspace = tmp_path
    agent.agent_mode = "review"
    agent.stream_callback = None
    agent.model = SimpleNamespace()
    agent.session = SimpleNamespace(
        compact=lambda: None,
        add_user_message=lambda *a, **k: None,
        add_assistant_message=lambda *a, **k: None,
    )
    agent.memory = SimpleNamespace()
    agent.conversation_manager = SimpleNamespace(finish_turn=lambda *args, **kwargs: None)
    agent._prepare_messages = lambda *args, **kwargs: ("prompt", [])
    return agent


def test_agent_run_publishes_fatal_turn_state(tmp_path):
    agent = _bare_agent(tmp_path)
    agent._invoke_with_messages = lambda messages: (_ for _ in ()).throw(
        RuntimeError("invalid model request")
    )

    response = agent.run("prompt")

    assert response == "执行出错: invalid model request"
    assert agent.last_turn_state.transition == TurnTransition.MODEL_ERROR
    assert agent.last_turn_state.error_message == "invalid model request"


def test_agent_run_marks_recoverable_retry_exhaustion(tmp_path, monkeypatch):
    agent = _bare_agent(tmp_path)
    attempts = []

    def connection_failure(messages):
        attempts.append(True)
        raise RuntimeError("connection reset")

    agent._invoke_with_messages = connection_failure
    monkeypatch.setattr("lib.agent_recovery.time.sleep", lambda delay: None)

    response = agent.run("prompt")

    assert len(attempts) == 4
    assert response == "执行出错: connection reset"
    assert agent.last_turn_state.transition == TurnTransition.MAX_RETRIES
    assert agent.last_turn_state.error_message == "connection reset"


def test_agent_stream_publishes_interrupted_state_without_replaying_partial_text(tmp_path):
    agent = _bare_agent(tmp_path)

    def broken_stream(messages):
        yield {"agent": {"messages": [SimpleNamespace(type="ai", content="partial")]}}
        raise RuntimeError("invalid stream request")

    agent._iter_agent_stream = broken_stream
    agent._invoke_with_messages = lambda messages: (_ for _ in ()).throw(
        RuntimeError("invalid stream request")
    )
    output = list(agent.stream_run("prompt"))

    assert output == ["partial"]
    assert agent.last_turn_state.transition == TurnTransition.STREAM_INTERRUPTED
    assert agent.last_turn_state.error_message == "invalid stream request"


def test_agent_stream_marks_recoverable_retry_exhaustion(tmp_path, monkeypatch):
    agent = _bare_agent(tmp_path)
    attempts = []

    def broken_stream(messages):
        attempts.append(True)
        raise RuntimeError("connection reset")
        yield  # pragma: no cover - keep this function as a generator

    agent._iter_agent_stream = broken_stream
    agent._invoke_with_messages = lambda messages: "must not fallback"
    monkeypatch.setattr("lib.agent_recovery.time.sleep", lambda delay: None)

    output = list(agent.stream_run("prompt"))

    assert len(attempts) == 4
    assert output == ["执行出错: connection reset"]
    assert agent.last_turn_state.transition == TurnTransition.MAX_RETRIES
    assert agent.last_turn_state.error_message == "connection reset"


def test_agent_run_recovers_from_max_output_tokens(tmp_path):
    """max_output_tokens 恢复路径：注入「继续」消息后重试并成功。

    该路径此前零覆盖（agent.py 覆盖率 56%）。
    **性质：覆盖率测试，不是本轮改动的回归防护** —— 生产代码路径（agent.py 的
    max_output_tokens 分支）在 HEAD 上已存在，回退 lib/agent.py 后本测试仍通过。
    """
    agent = _bare_agent(tmp_path)
    seen = []

    def limited(messages):
        seen.append(list(messages))
        if len(seen) == 1:
            raise RuntimeError("max_output_tokens exceeded")
        return "resumed"

    agent._invoke_with_messages = limited

    response = agent.run("prompt")

    assert response == "resumed"
    assert agent._recovery_state["path"] == "max_output_tokens_recovery"
    # 第二次调用比第一次多一条「续写」消息
    assert len(seen[1]) == len(seen[0]) + 1
    assert "Resume directly" in seen[1][-1].content


def test_agent_run_recovers_from_prompt_too_long(tmp_path):
    """prompt_too_long 恢复路径：强制压缩并重建消息后重试。

    必须走 force_compact 而不是 compact —— compact() 在轮数不足时会直接返回且
    谎报「已压缩」，而「轮数少但单轮巨大」正是最常见的超限形态。
    **性质：覆盖率测试，不是本轮改动的回归防护** —— prompt_too_long 分支本身
    （agent.py 的 _force_compact_session + _build_messages 重建）在 HEAD 上已存在，
    回退 lib/agent.py 后本测试仍通过。本轮改动的是该分支失败时的错误呈现，
    见 test_agent_run_surfaces_compaction_failure / 对应 stream 测试。
    """
    agent = _bare_agent(tmp_path)
    compact_calls = []
    agent.session.force_compact = lambda reason="": compact_calls.append(True)
    agent._build_messages = lambda **kwargs: ["rebuilt"]
    calls = []

    def too_long(messages):
        calls.append(messages)
        if len(calls) == 1:
            raise RuntimeError("maximum context length exceeded")
        return "compacted"

    agent._invoke_with_messages = too_long

    response = agent.run("prompt")

    assert response == "compacted"
    assert agent._recovery_state["path"] == "compact_retry"
    assert compact_calls == [True]
    assert calls[1] == ["rebuilt"]


def test_agent_run_surfaces_compaction_failure_instead_of_silent_retry(tmp_path):
    """压缩失败必须记录**并呈现给用户**，而不是只写进 _recovery_state。

    压缩失败会让 prompt_too_long 恢复路径失效（重试带的仍是原样超限的消息），
    因此最终错误必须带上压缩失败原因；只报模型错误等于用户什么诊断信息都拿不到。
    **回归防护**：回退 _format_execution_error 接线（恢复 f"执行出错: {error_msg}"）
    后本测试失败。
    """
    agent = _bare_agent(tmp_path)

    def broken_compact():
        raise RuntimeError("compaction backend unavailable")

    agent.session.force_compact = lambda reason="": broken_compact()
    agent._invoke_with_messages = lambda messages: (_ for _ in ()).throw(
        RuntimeError("maximum context length exceeded")
    )

    response = agent.run("prompt")

    # 状态仍然记录，供程序化诊断
    assert agent._recovery_state["compact_error"] == "compaction backend unavailable"
    # 且必须真的呈现给用户，保留原有「执行出错」前缀
    assert response == (
        "执行出错: maximum context length exceeded"
        "（上下文压缩失败: compaction backend unavailable）"
    )
    assert agent.last_turn_state.transition == TurnTransition.MAX_RETRIES


def test_agent_stream_surfaces_compaction_failure_instead_of_silent_retry(tmp_path):
    """stream_run 的压缩失败同样必须呈现在用户可见的错误文本里。"""
    agent = _bare_agent(tmp_path)

    def broken_stream(messages):
        raise RuntimeError("maximum context length exceeded")
        yield  # pragma: no cover - keep this function as a generator

    agent._iter_agent_stream = broken_stream
    agent.session.force_compact = lambda reason="": (_ for _ in ()).throw(
        RuntimeError("compaction backend unavailable")
    )
    agent._invoke_with_messages = lambda messages: (_ for _ in ()).throw(
        RuntimeError("maximum context length exceeded")
    )

    output = list(agent.stream_run("prompt"))

    assert output == [
        "执行出错: maximum context length exceeded"
        "（上下文压缩失败: compaction backend unavailable）"
    ]
    assert agent._recovery_state["compact_error"] == "compaction backend unavailable"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
