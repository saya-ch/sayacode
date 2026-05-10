"""
拒绝追踪器 — 参考 Claude Code denialTracking.

追踪工具权限拒绝计数，支持连续拒绝和总计拒绝两个维度。
当连续拒绝达到阈值时自动回退到询问模式。

v2: 支持不可变状态模式 (immutable mode)。
在不可变模式下，record_denial/record_success 返回新实例，
原实例保持不变。这适用于子 Agent 场景，它们不应修改
父级的共享状态。

用法：
    # 可变模式（向后兼容）
    tracker = DenialTracker()
    tracker.record_denial()

    # 不可变模式（子 Agent）
    tracker = DenialTracker()
    next_tracker = tracker.record_denial(immutable=True)
    # tracker 保持不变，next_tracker 有更新后的状态
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DenialTracker:
    """追踪工具权限拒绝，实现自动回退策略。

    两个计数器：
    - consecutive_denials: 连续拒绝，任何成功恢复为零
    - total_denials: 总计拒绝，单调递增

    阈值：
    - 3 次连续拒绝 → 回退
    - 20 次总计拒绝 → 回退

    模式：
    - 可变模式 (默认)：方法就地修改实例
    - 不可变模式：方法返回新实例，原实例不变
    """

    consecutive_denials: int = 0
    total_denials: int = 0
    MAX_CONSECUTIVE: int = field(default=3, repr=False)
    MAX_TOTAL: int = field(default=20, repr=False)
    _is_fallback_mode: bool = False

    def record_denial(self, immutable: bool = False) -> "DenialTracker":
        """记录一次拒绝。

        Args:
            immutable: True 时返回新实例，原实例不变。
        """
        if immutable:
            return DenialTracker(
                consecutive_denials=self.consecutive_denials + 1,
                total_denials=self.total_denials + 1,
                MAX_CONSECUTIVE=self.MAX_CONSECUTIVE,
                MAX_TOTAL=self.MAX_TOTAL,
                _is_fallback_mode=self._is_fallback_mode,
            )
        self.consecutive_denials += 1
        self.total_denials += 1
        return self

    def record_success(self, immutable: bool = False) -> "DenialTracker":
        """记录一次允许，重置连续计数。

        Args:
            immutable: True 时返回新实例，原实例不变。
        """
        if immutable:
            return DenialTracker(
                consecutive_denials=0,
                total_denials=self.total_denials,
                MAX_CONSECUTIVE=self.MAX_CONSECUTIVE,
                MAX_TOTAL=self.MAX_TOTAL,
                _is_fallback_mode=self._is_fallback_mode,
            )
        self.consecutive_denials = 0
        return self

    def should_fallback_to_prompting(self) -> bool:
        """是否应回退到询问模式。"""
        if self._is_fallback_mode:
            return False
        return (
            self.consecutive_denials >= self.MAX_CONSECUTIVE
            or self.total_denials >= self.MAX_TOTAL
        )

    def enter_fallback_mode(self, immutable: bool = False) -> "DenialTracker":
        """进入回退模式（每个操作都需要用户确认）。"""
        if immutable:
            return DenialTracker(
                consecutive_denials=0,
                total_denials=self.total_denials,
                MAX_CONSECUTIVE=self.MAX_CONSECUTIVE,
                MAX_TOTAL=self.MAX_TOTAL,
                _is_fallback_mode=True,
            )
        self._is_fallback_mode = True
        self.consecutive_denials = 0
        return self

    def exit_fallback_mode(self, immutable: bool = False) -> "DenialTracker":
        """退出回退模式。"""
        if immutable:
            return DenialTracker(
                consecutive_denials=0,
                total_denials=self.total_denials,
                MAX_CONSECUTIVE=self.MAX_CONSECUTIVE,
                MAX_TOTAL=self.MAX_TOTAL,
                _is_fallback_mode=False,
            )
        self._is_fallback_mode = False
        self.consecutive_denials = 0
        return self

    @property
    def is_in_fallback(self) -> bool:
        """当前是否在回退模式中。"""
        return self._is_fallback_mode

    @property
    def summary(self) -> str:
        """人类可读的状态摘要。"""
        parts = [
            f"连续拒绝: {self.consecutive_denials}/{self.MAX_CONSECUTIVE}",
            f"总计拒绝: {self.total_denials}/{self.MAX_TOTAL}",
        ]
        if self._is_fallback_mode:
            parts.append("（回退模式）")
        return ", ".join(parts)

    def reset(self) -> None:
        """完全重置状态（仅可变模式）。"""
        self.consecutive_denials = 0
        self.total_denials = 0
        self._is_fallback_mode = False

    def snapshot(self) -> dict:
        """返回状态的不可变快照（用于序列化/审计）。"""
        return {
            "consecutive_denials": self.consecutive_denials,
            "total_denials": self.total_denials,
            "is_fallback_mode": self._is_fallback_mode,
            "max_consecutive": self.MAX_CONSECUTIVE,
            "max_total": self.MAX_TOTAL,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "DenialTracker":
        """从快照恢复状态（用于子 Agent 初始化时继承父级状态）。"""
        return cls(
            consecutive_denials=data.get("consecutive_denials", 0),
            total_denials=data.get("total_denials", 0),
            MAX_CONSECUTIVE=data.get("max_consecutive", 3),
            MAX_TOTAL=data.get("max_total", 20),
            _is_fallback_mode=data.get("is_fallback_mode", False),
        )


__all__ = ["DenialTracker"]
