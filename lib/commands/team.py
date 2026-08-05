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

    def __init__(self) -> None:
        self._managers: dict[str, TeamManager] = {}

    def _manager(self) -> TeamManager:
        from lib.core.paths import SayacodePaths

        base_dir = SayacodePaths.resolve().home
        key = str(base_dir)
        if key not in self._managers:
            self._managers[key] = TeamManager(base_dir)
        return self._managers[key]

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        args = command.args.strip().split(maxsplit=2)
        sub = args[0].lower() if args else "status"

        tm = self._manager()

        if sub == "spawn" and len(args) >= 3:
            agent_type = args[1]
            task = args[2]
            try:
                worker_id = tm.spawn(agent_type, task, workspace=str(runtime.workspace))
            except (ValueError, RuntimeError, OSError) as exc:
                console.print(f"[red]子 Agent 启动失败[/]: {exc}")
                return True
            state = tm.get_worker_state(worker_id)
            status = state.status.value if state else "failed"
            console.print(f"[green]子 Agent 已提交[/]: {worker_id} (类型: {agent_type}, 状态: {status})")
            if state and state.worktree:
                console.print(f"  隔离分支: {state.config.get('branch')}")
                console.print(f"  Worktree: {state.worktree}")
            console.print(f"  使用 /team wait {worker_id} 等待，或 /team result {worker_id} 查看结果")
            return True

        if sub == "result" and len(args) >= 2:
            worker_id = args[1]
            state = tm.get_worker_state(worker_id)
            if state is None:
                console.print(f"[red]未知子 Agent[/]: {worker_id}")
                return True
            result = tm.get_result(worker_id)
            if result is None:
                console.print(f"{worker_id} 尚未完成，当前状态: {state.status.value}")
                return True
            if result.get("ok"):
                console.print(str(result.get("response") or "(空结果)"), markup=False)
                if result.get("worktree"):
                    console.print(f"交付分支: {result.get('branch')}\nWorktree: {result.get('worktree')}")
            else:
                console.print(f"[red]{worker_id} 执行失败[/]: {result.get('error') or 'unknown error'}")
            return True

        if sub == "wait" and len(args) >= 2:
            worker_id = args[1]
            try:
                timeout = float(args[2]) if len(args) >= 3 else 60.0
            except ValueError:
                console.print("[red]等待秒数必须是数字[/]")
                return True
            if tm.get_worker_state(worker_id) is None:
                console.print(f"[red]未知子 Agent[/]: {worker_id}")
                return True
            result = tm.wait(worker_id, timeout=timeout)
            if result is None:
                state = tm.get_worker_state(worker_id)
                status = state.status.value if state else "unknown"
                console.print(f"等待超时，当前状态: {status}")
            elif result.get("ok"):
                console.print(str(result.get("response") or "(空结果)"), markup=False)
                if result.get("worktree"):
                    console.print(f"交付分支: {result.get('branch')}\nWorktree: {result.get('worktree')}")
            else:
                console.print(f"[red]{worker_id} 执行失败[/]: {result.get('error') or 'unknown error'}")
            return True

        if sub == "diff" and len(args) >= 2:
            worker_id = args[1]
            try:
                delivery = tm.get_delivery(worker_id)
            except (ValueError, RuntimeError, OSError) as exc:
                console.print(f"[red]无法检查交付[/]: {exc}")
                return True
            if delivery is None:
                console.print(f"{worker_id} 没有隔离 Worktree")
                return True
            sections = [
                f"分支: {delivery['branch']}",
                f"Worktree: {delivery['worktree']}",
                "状态:",
                delivery["status"] or "(clean)",
                "Diff 统计:",
                delivery["diff_stat"] or "(none)",
                "新增提交:",
                delivery["commits"] or "(none)",
            ]
            console.print("\n".join(sections), markup=False)
            return True

        if sub == "cleanup":
            count = tm.cleanup()
            console.print(f"已终止 {count} 个子 Agent")
            return True

        if sub in {"status", "list"}:
            console.print(tm.get_status())
            return True

        console.print(
            "用法: /team status | /team spawn <builder|shared-builder|planner|reviewer> <任务> | "
            "/team wait <worker-id> [秒数] | /team result <worker-id> | "
            "/team diff <worker-id> | /team cleanup"
        )
        return True
