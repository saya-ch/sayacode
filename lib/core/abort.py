"""同级工具中止信号：只杀同批次剩余调用，不结束整轮。

整轮是否结束由调用配额中间件决定，两者正交。
本模块只依赖标准库，core 与 tools 共用。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ToolAbortController:
    """同级中止控制器，参考 Claude Code 的 siblingAbort 思路。

    Bash 和 Shell 与 Git 类工具失败时，向同级工具发送中止信号。
    用法是失败方调 abort，别的工具执行前查 is_aborted。
    """

    _aborted: bool = False
    _reason: str = ""

    def abort(self, reason: str) -> None:
        """记下中止信号，由失败的工具调用."""
        self._aborted = True
        self._reason = reason

    @property
    def is_aborted(self) -> bool:
        """是否已收到中止信号."""
        return self._aborted

    @property
    def reason(self) -> str:
        """中止原因，缺省是 unknown."""
        return self._reason or "unknown"

    def reset(self) -> None:
        """清掉中止状态，每批工具执行前调一次."""
        self._aborted = False
        self._reason = ""


_ABORT_CONTROLLER: ContextVar[ToolAbortController] = ContextVar(
    "_sayacode_abort_controller", default=ToolAbortController()
)


def get_abort_controller() -> ToolAbortController:
    """拿当前上下文的中止控制器."""
    return _ABORT_CONTROLLER.get()


def set_abort_controller(ctrl: ToolAbortController) -> None:
    """换当前上下文的中止控制器."""
    _ABORT_CONTROLLER.set(ctrl)


__all__ = [
    "ToolAbortController",
    "get_abort_controller",
    "set_abort_controller",
]
