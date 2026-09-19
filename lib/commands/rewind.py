"""Rewind slash 命令：把会话与图状态一起退回到某个轮次边界。

/rewind 列出可回退的检查点；/rewind <n> 回退最近 n 轮。回退同时作用于
LangGraph 检查点（从旧 checkpoint 分叉）与 SessionManager（截断到同样的
轮数）——两者必须一致，否则下次在新进程里全量导入会把回退覆盖掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import print_error, print_info, print_success
from .base import CommandContext, CommandHandler


def _fmt_time(value: str | object | None) -> str:
    """ISO 时间串取到秒；不是 ISO 形状就原样返回。"""
    text = str(value or "")
    if "T" in text:
        return text.split("T", 1)[1][:8]
    return text[:8] or "-"


def _truncate_session_to_turns(session: Any, target: int) -> int:
    # 用 Any 承接会话动态对象
    """截断会话到目标用户轮次（优先调 core 方法，缺失则本地截断）。"""
    truncate = getattr(session, "truncate_to_user_turns", None)
    if callable(truncate):
        return int(truncate(target) or 0)
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return 0
    # 数 user 轮：按 user 起轮，assistant 收尾。
    user_idx = [i for i, m in enumerate(messages) if getattr(m, "role", None) == "user"]
    if target < 0:
        return 0
    if len(user_idx) <= target:
        return 0
    if target <= 0:
        dropped = len(messages)
        messages.clear()
    else:
        end = user_idx[target] if target < len(user_idx) else len(messages)
        dropped = len(messages) - end
        del messages[end:]
    try:
        rebuild = getattr(session, "_rebuild_token_count", None)
        if callable(rebuild):
            rebuild()
    except Exception:
        pass
    return int(dropped)


def _truncate_memory_to_turns(memory: Any, target: int) -> int:
    # 用 Any 保持旧调用兼容
    """记忆截断已废弃：历史唯一真相源为 session（上一步已截断），此处恒为 no-op。

    保留函数名供旧调用方兼容；始终返回 0。
    """
    return 0


@dataclass
class RewindCommandHandler(CommandHandler):
    """回退命令处理器：图检查点与本机镜像一起退回。"""

    name: str = "rewind"
    aliases: tuple[str, ...] = ("undo",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        """处理 slash 命令，已被消费时返回 True。"""
        agent = runtime.agent
        runner = getattr(agent, "runner", None)
        if runner is None:
            print_error(tr("rewind.unsupported"))
            return True

        raw = command.args.strip()
        current = runner.current_turn_count()
        if not raw:
            self._print_points(runner, current)
            return True

        try:
            count = int(raw)
        except ValueError:
            print_error(tr("rewind.invalid_arg", value=raw))
            return True
        if count <= 0:
            print_error(tr("rewind.invalid_arg", value=raw))
            return True
        if count > current:
            print_error(tr("rewind.too_many", turns=current))
            return True

        target = current - count
        # 回退图检查点并截断会话，保持两者一致。
        if not runner.rewind_to_turn_count(target):
            print_error(tr("rewind.unsupported"))
            return True
        dropped = _truncate_session_to_turns(agent.session, target)
        print_success(tr("rewind.done", undone=count, turns=target, dropped=dropped))
        memory = getattr(agent, "memory", None)
        forgotten = _truncate_memory_to_turns(memory, target)
        if forgotten:
            print_info(tr("rewind.memory", count=forgotten))
        return True

    def _print_points(self, runner: Any, current: int) -> None:
        # 用 Any 承接运行器动态对象
        """列出可回退的检查点。"""
        points = runner.list_rewind_points()
        if not points:
            print_info(tr("rewind.empty"))
            return
        print_info(tr("rewind.current", turns=current))
        print_info(tr("rewind.list_title"))
        for point in points:
            print_info(
                tr(
                    "rewind.point",
                    turns=point["turns"],
                    time=_fmt_time(point["created_at"]),
                )
            )
        print_info(tr("rewind.usage"))


__all__ = ["RewindCommandHandler"]
