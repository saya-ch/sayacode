"""P1: 拒绝追踪器测试."""

import pytest
from lib.core.denial_tracker import DenialTracker


class TestDenialTracker:
    def test_initial_state(self):
        dt = DenialTracker()
        assert dt.consecutive_denials == 0
        assert dt.total_denials == 0
        assert not dt.is_in_fallback
        assert not dt.should_fallback_to_prompting()

    def test_single_denial_not_enough(self):
        dt = DenialTracker()
        dt.record_denial()
        assert dt.consecutive_denials == 1
        assert dt.total_denials == 1
        assert not dt.should_fallback_to_prompting()

    def test_consecutive_denials_trigger(self):
        dt = DenialTracker(MAX_CONSECUTIVE=3)
        dt.record_denial()
        dt.record_denial()
        assert not dt.should_fallback_to_prompting()
        dt.record_denial()  # 第 3 次
        assert dt.should_fallback_to_prompting()

    def test_success_resets_consecutive(self):
        dt = DenialTracker()
        dt.record_denial()
        dt.record_denial()
        assert dt.consecutive_denials == 2
        dt.record_success()
        assert dt.consecutive_denials == 0

    def test_total_denials_monotonic(self):
        """总计拒绝单调递增，不因成功而重置。"""
        dt = DenialTracker()
        dt.record_denial()
        dt.record_success()
        dt.record_denial()
        assert dt.total_denials == 2
        assert dt.consecutive_denials == 1

    def test_total_denials_trigger(self):
        # 设置较高的连续阈值，仅测试总计阈值
        dt = DenialTracker(MAX_CONSECUTIVE=50, MAX_TOTAL=20)
        for _ in range(19):
            dt.record_denial()
        assert not dt.should_fallback_to_prompting()
        dt.record_denial()  # 第 20 次
        assert dt.should_fallback_to_prompting()

    def test_fallback_mode_blocks_retrigger(self):
        dt = DenialTracker()
        # 触发回退
        for _ in range(3):
            dt.record_denial()
        assert dt.should_fallback_to_prompting()
        dt.enter_fallback_mode()
        # 在回退模式下不应再触发
        assert not dt.should_fallback_to_prompting()

    def test_exit_fallback(self):
        dt = DenialTracker()
        dt.enter_fallback_mode()
        assert dt.is_in_fallback
        dt.exit_fallback_mode()
        assert not dt.is_in_fallback
        assert dt.consecutive_denials == 0

    def test_full_reset(self):
        dt = DenialTracker()
        for _ in range(5):
            dt.record_denial()
        dt.enter_fallback_mode()
        dt.reset()
        assert dt.consecutive_denials == 0
        assert dt.total_denials == 0
        assert not dt.is_in_fallback

    def test_summary_string(self):
        dt = DenialTracker()
        dt.record_denial()
        s = dt.summary
        assert "连续拒绝" in s
        assert "1/3" in s
        assert "总计拒绝" in s

    def test_custom_thresholds(self):
        dt = DenialTracker(MAX_CONSECUTIVE=5, MAX_TOTAL=50)
        assert dt.MAX_CONSECUTIVE == 5
        assert dt.MAX_TOTAL == 50
        for _ in range(4):
            dt.record_denial()
        assert not dt.should_fallback_to_prompting()
        dt.record_denial()  # 第 5 次
        assert dt.should_fallback_to_prompting()

    def test_single_denial_no_fallback_then_success(self):
        """单次拒绝后成功，不应触发回退。"""
        dt = DenialTracker()
        dt.record_denial()
        dt.record_success()
        dt.record_denial()
        dt.record_success()
        dt.record_denial()
        dt.record_success()
        assert not dt.should_fallback_to_prompting()
        assert dt.consecutive_denials == 0
        assert dt.total_denials == 3


class TestDenialTrackerImmutable:
    """不可变模式测试。"""

    def test_immutable_denial_returns_new(self):
        dt = DenialTracker()
        dt2 = dt.record_denial(immutable=True)
        assert dt.consecutive_denials == 0  # 原实例不变
        assert dt2.consecutive_denials == 1
        assert dt2 is not dt

    def test_immutable_success_returns_new(self):
        dt = DenialTracker()
        dt.record_denial()  # mutable: 0→1
        dt2 = dt.record_success(immutable=True)
        assert dt.consecutive_denials == 1  # 原实例不变
        assert dt2.consecutive_denials == 0

    def test_immutable_fallback_returns_new(self):
        dt = DenialTracker()
        for _ in range(3):
            dt.record_denial()
        dt2 = dt.enter_fallback_mode(immutable=True)
        assert not dt.is_in_fallback  # 原实例不变
        assert dt2.is_in_fallback

    def test_immutable_exit_fallback_returns_new(self):
        dt = DenialTracker()
        dt.enter_fallback_mode()
        dt2 = dt.exit_fallback_mode(immutable=True)
        assert dt.is_in_fallback  # 原实例不变
        assert not dt2.is_in_fallback

    def test_snapshot_and_restore(self):
        dt = DenialTracker()
        dt.record_denial()
        dt.record_denial()
        snap = dt.snapshot()
        assert snap["consecutive_denials"] == 2
        assert snap["total_denials"] == 2
        assert not snap["is_fallback_mode"]

        restored = DenialTracker.from_snapshot(snap)
        assert restored.consecutive_denials == 2
        assert restored.total_denials == 2

    def test_snapshot_preserves_fallback(self):
        dt = DenialTracker()
        dt.enter_fallback_mode()
        snap = dt.snapshot()
        assert snap["is_fallback_mode"]
        restored = DenialTracker.from_snapshot(snap)
        assert restored.is_in_fallback

    def test_immutable_chain(self):
        """不可变模式下链式调用——适合函数式状态更新。"""
        dt = DenialTracker()
        dt = dt.record_denial(immutable=True)
        dt = dt.record_denial(immutable=True)
        dt = dt.record_denial(immutable=True)
        assert dt.should_fallback_to_prompting()
        assert dt.consecutive_denials == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
