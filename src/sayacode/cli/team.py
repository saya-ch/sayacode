"""continuable 子 Agent 的 `/team` 命令。"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from ..agent.events import action_requests
from ..tasks import TaskError

if TYPE_CHECKING:
    from ..application import SayacodeApp


async def team_command(app: SayacodeApp, args: Any) -> Any:
    """解析任务查询、通信、控制和 worktree 交付命令。"""
    tokens = shlex.split(str(args or ""))
    action = tokens[0].lower() if tokens else "list"
    if action in {"list", "status"}:
        if action == "status" and len(tokens) == 2:
            return (await app.tasks.get(tokens[1])).to_dict()
        return [record.to_dict() for record in await app.tasks.list(workspace=app.workspace)]
    if action == "spawn":
        if len(tokens) < 3:
            raise ValueError("Usage: /team spawn <builder|planner|reviewer> <task>")
        return (
            await app._spawn_task(
                " ".join(tokens[2:]), role=tokens[1].lower(), parent_thread_id=app.session_id
            )
        ).to_dict()
    if action == "wait":
        if len(tokens) != 2:
            raise ValueError("Usage: /team wait <task-id>")
        return (await app.tasks.wait(tokens[1])).to_dict()
    if action == "pending":
        if len(tokens) != 2:
            raise ValueError("Usage: /team pending <task-id>")
        record = await app.tasks.get(tokens[1])
        if record.status != "paused":
            return {"task_id": record.task_id, "status": record.status, "action_requests": []}
        handle, _ = await app._context_for_thread(record.thread_id)
        state = await app.runtime.get_state(handle, record.thread_id)
        return {
            "task_id": record.task_id,
            "thread_id": record.thread_id,
            "status": record.status,
            "action_requests": action_requests(list(state.interrupts)),
            "trust_level": record.trust_level,
        }
    if action == "stop":
        if len(tokens) != 2:
            raise ValueError("Usage: /team stop <task-id>")
        return (await app.tasks.stop(tokens[1])).to_dict()
    if action == "resume":
        if len(tokens) != 2:
            raise ValueError("Usage: /team resume <task-id>")
        record = await app.tasks.get(tokens[1])
        if record.status == "paused":
            raise TaskError("Task is waiting for approval; approve or reject its pending action")
        if record.status == "failed":
            raise TaskError("Failed tasks require /team followup with a new instruction")
        return (await app.tasks.resume(record.task_id, app._task_runner)).to_dict()
    if action in {"followup", "follow-up"}:
        if len(tokens) < 3:
            raise ValueError("Usage: /team followup <task-id> <message>")
        record = await app.tasks.get(tokens[1])
        if record.status == "paused":
            raise TaskError("Task is waiting for approval; approve or reject before a follow-up")
        await app.task_inbox.send(
            sender_thread_id=app.session_id,
            receiver_thread_id=record.thread_id,
            task_id=record.task_id,
            kind="user_followup",
            content=" ".join(tokens[2:]),
        )
        return (await app.tasks.get(record.task_id)).to_dict()
    if action in {"diff", "delivery"}:
        if len(tokens) != 2:
            raise ValueError("Usage: /team diff <task-id>")
        return await app.tasks.delivery(tokens[1])
    if action == "apply":
        if len(tokens) != 2:
            raise ValueError("Usage: /team apply <task-id>")
        return await app.tasks.apply_delivery(tokens[1])
    if action == "cleanup":
        if len(tokens) != 2:
            raise ValueError("Usage: /team cleanup <task-id>")
        return (await app.tasks.remove_worktree(tokens[1])).to_dict()
    raise ValueError("Usage: /team [list|spawn|wait|stop|resume|followup|diff|apply|cleanup]")


__all__ = ["team_command"]
