"""P0: Agent 循环鲁棒性测试 — TurnTransition, TurnState, ToolAbortController."""

import pytest
from lib.core.agent_runtime import TurnTransition, TurnState
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
