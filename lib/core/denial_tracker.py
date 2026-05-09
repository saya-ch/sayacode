"""
拒绝追踪器 — 参考 Claude Code denialTracking.

追踪工具权限拒绝计数，在连续拒绝达到阈值时自动回退到询问模式。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DenialTracker:
    """追踪工具权限拒绝，实现自动回退策略。

    两个计数器：
    - consecutive_denials: 连续拒绝，任何成功恢复为零
    - total_denials: 总计拒绝，单调递增

    阈值：
    - 3 次连续拒绝 → 回退
    - 20 次总计拒绝 → 回退
    """
    consecutive_denials: int = 0
    total_denials: int = 0
    MAX_CONSECUTIVE: int = field(default=3, repr=False)
    MAX_TOTAL: int = field(default=20, repr=False)
    _is_fallback_mode: bool = False

    def record_denial(self) -> None:
        """记录一次拒绝。"""
        self.consecutive_denials += 1
        self.total_denials += 1

    def record_success(self) -> None:
        """记录一次允许，重置连续计数。"""
        self.consecutive_denials = 0

    def should_fallback_to_prompting(self) -> bool:
        """是否应回退到询问模式。"""
        if self._is_fallback_mode:
            return False  # 已经在回退模式，不要重复触发
        return (
            self.consecutive_denials >= self.MAX_CONSECUTIVE
            or self.total_denials >= self.MAX_TOTAL
        )

    def enter_fallback_mode(self) -> None:
        """进入回退模式（每个操作都需要用户确认）。"""
        self._is_fallback_mode = True
        # 进入回退模式时重置计数，避免循环触发
        self.consecutive_denials = 0

    def exit_fallback_mode(self) -> None:
        """退出回退模式。"""
        self._is_fallback_mode = False
        self.consecutive_denials = 0

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
        """完全重置状态。"""
        self.consecutive_denials = 0
        self.total_denials = 0
        self._is_fallback_mode = False


__all__ = ["DenialTracker"]
