"""计划类 slash command：查看自主计划表。

职责：读取当前会话的计划并渲染为表格。
核心类：PlanCommandHandler。
调用链位置：router → 本模块 → lib.core.plans。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.plans import PlanStore
from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import print_info, print_plan_table
from .base import CommandContext, CommandHandler


@dataclass
class PlanCommandHandler(CommandHandler):
    """展示当前会话的自主计划表。"""

    name: str = "plan"
    aliases: tuple[str, ...] = ("plans",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        """处理 slash 命令，已被消费时返回 True。"""
        state = runtime.app_state
        session_id = "default"
        if state is not None and getattr(state, "session", None) is not None:
            session_id = getattr(state.session, "session_id", "default")
        plan = PlanStore(runtime.workspace, session_id).get()
        if plan is None:
            print_info(tr("plan.empty"))
            return True
        print_plan_table(plan)
        return True
