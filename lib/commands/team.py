"""/team 命令，提供多 Agent 协作。

支持 spawn、wait、result、diff 与 cleanup 子命令，核心类为
TeamCommandHandler，经 router 分发并调用 TeamSupervisor（同步执行）。
spawn 为同步执行：返回时结果已就绪，wait/result 直接读取。
"""

from __future__ import annotations

from pathlib import Path

from lib.core.team_supervisor import TeamSupervisor
from lib.cli.theme import console
from .base import CommandContext
from ..runtime import RuntimeContext


def _status_of(state: object) -> str:
    """兼容 dict 与 WorkerState 两种状态形状。"""
    if state is None:
        return "unknown"
    if isinstance(state, dict):
        status = state.get("status", "unknown")
        try:
            return str(getattr(status, "value", status) or "unknown")
        except Exception:
            return "unknown"
    status = getattr(state, "status", "unknown")
    try:
        return str(getattr(status, "value", status) or "unknown")
    except Exception:
        return "unknown"


def _worktree_of(state: object) -> str:
    """取 worktree（缺失返回空串）。"""
    if state is None:
        return ""
    if isinstance(state, dict):
        return str(state.get("worktree") or "")
    return str(getattr(state, "worktree", "") or "")


def _branch_of(state: object) -> str:
    """取交付分支（config.branch 优先，缺失返回空串）。"""
    if state is None:
        return ""
    if isinstance(state, dict):
        config = state.get("config") if isinstance(state.get("config"), dict) else {}
        return str((config or {}).get("branch") or state.get("branch") or "")
    config = getattr(state, "config", None)
    if isinstance(config, dict) and config.get("branch"):
        return str(config.get("branch"))
    return str(getattr(state, "branch", "") or "")


class TeamCommandHandler:
    """处理 /team 命令。"""
    name = "team"
    aliases: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._supervisors: dict[str, TeamSupervisor] = {}

    def _supervisor(self, runtime: RuntimeContext) -> TeamSupervisor:
        """按 home 目录缓存 supervisor（模型/工具取自当前 runtime）。"""
        from lib.core.paths import SayacodePaths

        home = SayacodePaths.resolve().home
        try:
            tools = list(getattr(runtime, "tools", []) or [])
        except Exception:
            tools = []
        key = str(home)
        supervisor = self._supervisors.get(key)
        if supervisor is None:
            supervisor = TeamSupervisor(
                model=runtime.model,
                workspace=Path(str(runtime.workspace)),
                runtime=runtime,
                tools=tools,
                home=Path(str(home)),
            )
            self._supervisors[key] = supervisor
        return supervisor

    @staticmethod
    def _needs_isolation(agent_type: str) -> bool:
        """写入型子 Agent 隔离（shared-builder 显式承担共享工作区风险）。"""
        return "shared" not in str(agent_type).lower()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        args = command.args.strip().split(maxsplit=2)
        sub = args[0].lower() if args else "status"

        tm = self._supervisor(runtime)

        if sub == "spawn" and len(args) >= 3:
            agent_type = args[1]
            task = args[2]
            try:
                # 写入型先建隔离 worktree，跑完把交付信息挂回 worker 记录。
                if self._needs_isolation(agent_type):
                    from lib.core.team_supervisor import new_worker_id

                    worker_id = new_worker_id()
                    worktree = tm.worktrees.prepare(worker_id, str(runtime.workspace))
                    worker_id = tm.spawn(
                        agent_type, task,
                        workspace=worktree.workspace,
                        worker_id=worker_id,
                    )
                    tm.attach_worktree(
                        worker_id,
                        worktree=worktree.workspace,
                        branch=worktree.branch,
                        source_commit=worktree.source_commit,
                    )
                else:
                    worker_id = tm.spawn(agent_type, task, workspace=str(runtime.workspace))
            except (ValueError, RuntimeError, OSError) as exc:
                console.print(f"[red]子 Agent 启动失败[/]: {exc}")
                return True
            state = tm.get_worker_state(worker_id)
            status = _status_of(state) if state is not None else "failed"
            console.print(f"[green]子 Agent 已提交[/]: {worker_id} (类型: {agent_type}, 状态: {status})")
            worktree = _worktree_of(state)
            branch = _branch_of(state)
            if state is not None and worktree:
                console.print(f"  隔离分支: {branch}")
                console.print(f"  Worktree: {worktree}")
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
                console.print(f"{worker_id} 尚未完成，当前状态: {_status_of(state)}")
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
                status = _status_of(state)
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
