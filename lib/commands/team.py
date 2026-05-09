"""/team 命令 — 多 Agent 协作。"""

from __future__ import annotations

from lib.core.team_manager import TeamManager
from lib.theme import console
from .base import CommandContext
from ..runtime import RuntimeContext


class TeamCommandHandler:
    """处理 /team 命令。"""
    name = "team"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        args = command.args.strip().split(maxsplit=2)
        sub = args[0].lower() if args else "status"

        from lib.core.paths import SayacodePaths
        base_dir = SayacodePaths.resolve().home
        tm = TeamManager(base_dir)

        if sub == "spawn" and len(args) >= 3:
            agent_type = args[1]
            task = args[2]
            worker_id = tm.spawn(agent_type, task, workspace=str(runtime.workspace))
            console.print(f"[green]子 Agent 已启动[/]: {worker_id} (类型: {agent_type})")
            console.print(f"  任务: {task}")
            return True

        if sub == "cleanup":
            count = tm.cleanup()
            console.print(f"已终止 {count} 个子 Agent")
            return True

        # 默认: status
        console.print(tm.get_status())
        return True
