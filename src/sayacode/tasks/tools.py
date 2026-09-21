"""continuable 子 Agent 的模型可调用工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from ..agent.events import _message_text

if TYPE_CHECKING:
    from ..application import SayacodeApp


def parent_tools(app: SayacodeApp) -> list[BaseTool]:
    """创建父 Agent 使用的委派、通信和查询工具。"""

    @tool
    async def delegate_to_subagent(
        task: str,
        runtime: ToolRuntime[Any],
        role: Literal["builder", "planner", "reviewer"] = "planner",
        use_worktree: bool | None = None,
    ) -> dict[str, Any]:
        """派发一个独立、可继续对话的后台子 Agent，并立即返回其标识。

        builder 默认使用 Git worktree；明确传入 false 才共享父工作区。
        planner 和 reviewer 始终不创建 worktree。
        """
        context = runtime.context
        state = runtime.state if isinstance(runtime.state, dict) else {}
        messages = list(state.get("messages", []))
        user_goal = next(
            (
                _message_text(message)
                for message in reversed(messages)
                if getattr(message, "type", None) == "human"
                and getattr(message, "additional_kwargs", {}).get("sayacode_source")
                != "agent_inbox"
            ),
            "",
        )
        record = await app._spawn_task(
            task,
            role=role,
            parent_thread_id=context.session_id,
            profile_name=context.profile_name,
            use_worktree=use_worktree,
            context_snapshot={
                "user_goal": user_goal[:8_000],
                "parent_plan": list(state.get("todos", [])),
                "delegated_task": task,
                "role": role,
                "use_worktree": use_worktree if role == "builder" else False,
            },
        )
        return {
            "task_id": record.task_id,
            "thread_id": record.thread_id,
            "status": record.status,
            "workspace": record.task_workspace or record.workspace,
            "worktree_enabled": record.worktree_enabled,
            "workspace_mode": "worktree" if record.worktree_enabled else "shared",
        }

    @tool
    async def send_message_to_subagent(
        task_id: str, message: str, runtime: ToolRuntime[Any]
    ) -> dict[str, Any]:
        """向当前父 Agent 的直接子 Agent 发送追加要求或上下文。"""
        record = await app.tasks.get(task_id)
        if record.parent_thread_id != runtime.context.session_id:
            raise ValueError("Task is not a direct child of this Agent")
        if record.delivery_state == "cleaned":
            raise ValueError("Cleaned child cannot receive messages")
        sent = await app.task_inbox.send(
            sender_thread_id=runtime.context.session_id,
            receiver_thread_id=record.thread_id,
            task_id=task_id,
            kind="parent_message",
            content=message,
        )
        return {"message_id": sent.message_id, "task_id": task_id, "status": "queued"}

    @tool
    async def task_status(task_id: str, runtime: ToolRuntime[Any]) -> dict[str, Any]:
        """读取子 Agent 的状态、最近结果和交付元数据。"""
        record = await app.tasks.get(task_id)
        if record.parent_thread_id != runtime.context.session_id:
            raise ValueError("Task is not a direct child of this Agent")
        await app.task_inbox.acknowledge_task(runtime.context.session_id, task_id)
        return record.to_dict()

    @tool
    async def task_delivery(task_id: str, runtime: ToolRuntime[Any]) -> dict[str, Any]:
        """查看 builder 工作树相对派发快照的交付差异，不自动应用。"""
        record = await app.tasks.get(task_id)
        if record.parent_thread_id != runtime.context.session_id:
            raise ValueError("Task is not a direct child of this Agent")
        return await app.tasks.delivery(task_id)

    @tool
    async def task_wait(
        task_id: str, runtime: ToolRuntime[Any], timeout_seconds: float = 30
    ) -> dict[str, Any]:
        """限时等待子 Agent 当前轮次结束，超时后返回即时状态。"""
        if not 0 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        record = await app.tasks.get(task_id)
        if record.parent_thread_id != runtime.context.session_id:
            raise ValueError("Task is not a direct child of this Agent")
        records = await app.tasks.wait_active([task_id], timeout=timeout_seconds)
        settled = records[0]
        if settled.status not in {"pending", "running", "stopping"}:
            await app.task_inbox.acknowledge_task(runtime.context.session_id, task_id)
        return settled.to_dict()

    return [
        delegate_to_subagent,
        send_message_to_subagent,
        task_status,
        task_delivery,
        task_wait,
    ]


def child_tools(app: SayacodeApp) -> list[BaseTool]:
    """创建子 Agent 向直接父 Agent 提前报告发现的工具。"""

    @tool
    async def report_to_parent(message: str, runtime: ToolRuntime[Any]) -> dict[str, Any]:
        """向直接父 Agent 发送会影响其下一步工作的自包含发现。"""
        task = await app._task_by_thread(runtime.context.session_id)
        if task is None or task.parent_thread_id is None:
            raise ValueError("Current Agent has no continuable parent")
        sent = await app.task_inbox.send(
            sender_thread_id=task.thread_id,
            receiver_thread_id=task.parent_thread_id,
            task_id=task.task_id,
            kind="subagent_message",
            content=message,
        )
        return {"message_id": sent.message_id, "status": "queued"}

    return [report_to_parent]


__all__ = ["child_tools", "parent_tools"]
