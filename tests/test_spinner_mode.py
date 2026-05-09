"""P2: SpinnerMode 状态机测试."""

import pytest
from lib.theme import SpinnerMode


class TestSpinnerMode:
    def test_all_modes_exist(self):
        assert SpinnerMode.THINKING == "thinking"
        assert SpinnerMode.TEXT == "text"
        assert SpinnerMode.TOOL_USE == "tool_use"
        assert SpinnerMode.TOOL_RESULT == "tool_result"
        assert SpinnerMode.IDLE == "idle"

    def test_is_valid(self):
        for mode in ["thinking", "text", "tool_use", "tool_result", "idle"]:
            assert SpinnerMode.is_valid(mode), f"{mode} should be valid"

    def test_is_not_valid(self):
        assert not SpinnerMode.is_valid("unknown")
        assert not SpinnerMode.is_valid("")
        assert not SpinnerMode.is_valid("running")

    def test_all_modes_returns_frozenset(self):
        modes = SpinnerMode.all_modes()
        assert isinstance(modes, frozenset)
        assert len(modes) == 5
        assert "thinking" in modes
        assert "text" in modes

    def test_mode_transitions(self):
        """验证典型的状态转换流: thinking → tool_use → tool_result → text → idle."""
        flow = [
            SpinnerMode.THINKING,
            SpinnerMode.TOOL_USE,
            SpinnerMode.TOOL_RESULT,
            SpinnerMode.TEXT,
            SpinnerMode.IDLE,
        ]
        for mode in flow:
            assert SpinnerMode.is_valid(mode)

    def test_no_duplicate_values(self):
        modes = [
            SpinnerMode.THINKING,
            SpinnerMode.TEXT,
            SpinnerMode.TOOL_USE,
            SpinnerMode.TOOL_RESULT,
            SpinnerMode.IDLE,
        ]
        assert len(modes) == len(set(modes))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
