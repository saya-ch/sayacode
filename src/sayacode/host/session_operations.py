"""会话维护与工作区扩展操作；执行真相仍归官方图和本地服务。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from langchain.agents.middleware import FilesystemFileSearchMiddleware, TodoListMiddleware
from langchain_core.runnables import RunnableConfig

from ..application import SayacodeApp
from ..tools import tool_catalog


class SessionOperations:
    """Web 宿主注入工作区和线程查找方法后即可复用的类型化操作。"""

    async def _app_for_workspace(self, workspace_id: str) -> SayacodeApp:
        raise NotImplementedError

    async def _app_for_thread(self, thread_id: str) -> SayacodeApp:
        raise NotImplementedError

    async def _publish_thread_change(
        self, thread_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    async def list_checkpoints(self, thread_id: str) -> list[dict[str, Any]]:
        app = await self._app_for_thread(thread_id)
        if app.model is not None:
            try:
                handle, _ = await app._context_for_thread(thread_id)
                history = await app.runtime.get_history(handle, thread_id, limit=100)
            except (KeyError, ValueError, RuntimeError):
                pass
            else:
                return [
                    {
                        "checkpoint_id": str(
                            snapshot.config.get("configurable", {}).get("checkpoint_id") or ""
                        ),
                        "next": list(snapshot.next),
                        "message_count": len(snapshot.values.get("messages", []))
                        if snapshot.values
                        else 0,
                        "created_at": snapshot.created_at,
                    }
                    for snapshot in history
                ]
        config = cast(RunnableConfig, app.runtime.thread_config(thread_id))
        return [
            {
                "checkpoint_id": str(saved.config.get("configurable", {}).get("checkpoint_id") or ""),
                "next": [],
                "message_count": len(saved.checkpoint.get("channel_values", {}).get("messages", [])),
                "created_at": saved.checkpoint.get("ts"),
            }
            async for saved in app.runtime.checkpointer.alist(config, limit=100)
        ]

    async def compact_thread(self, thread_id: str, focus: str | None = None) -> dict[str, Any]:
        app = await self._app_for_thread(thread_id)
        if app.model is None:
            raise ValueError("请先在模型设置中配置并选用模型")
        async with app._thread_lock(thread_id):
            handle, context = await app._context_for_thread(thread_id)
            compacted = await app.runtime.compact(
                handle, context, thread_id=thread_id, focus=focus
            )
        if compacted:
            await self._publish_thread_change(
                thread_id, "thread.compacted", {"focus": focus}
            )
        return {"compacted": compacted}

    async def rewind_thread(self, thread_id: str, checkpoint_id: str) -> dict[str, Any]:
        app = await self._app_for_thread(thread_id)
        if app.model is None:
            raise ValueError("请先在模型设置中配置并选用模型")
        async with app._thread_lock(thread_id):
            indexed = await app.runtime.get_thread(thread_id)
            if indexed is not None and indexed.get("status") == "running":
                raise RuntimeError("运行中不能回退检查点")
            handle, _ = await app._context_for_thread(thread_id)
            current = await app.runtime.get_state(handle, thread_id)
            if current.next or current.interrupts:
                raise RuntimeError("请先完成或处理当前待批准操作")
            fork = await app.runtime.rewind(handle, thread_id, checkpoint_id)
        await self._publish_thread_change(
            thread_id, "thread.rewound", {"checkpoint_id": checkpoint_id}
        )
        return {
            "rewound": True,
            "checkpoint_id": checkpoint_id,
            "fork_checkpoint_id": fork.get("configurable", {}).get("checkpoint_id"),
        }

    async def trace_thread(self, thread_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        app = await self._app_for_thread(thread_id)
        rows = await app.audit.list(thread_id=thread_id, limit=200)
        if run_id:
            rows = [
                row
                for row in rows
                if row.get("run_id") == run_id
                or (row.get("details") or {}).get("parent_run_id") == run_id
            ]
        return rows

    async def list_thread_tools(self, thread_id: str) -> list[dict[str, Any]]:
        app = await self._app_for_thread(thread_id)
        if app.model is None:
            return []
        try:
            _, context = await app._context_for_thread(thread_id)
        except (KeyError, ValueError, RuntimeError):
            return []
        include_team = context.task_id is None
        profile = app.config.profile(context.profile_name)
        explicit = app._tools_for_context(
            context, include_team_tools=include_team, profile=profile
        )
        official = list(TodoListMiddleware().tools)
        if profile.file_search:
            official.extend(
                FilesystemFileSearchMiddleware(root_path=str(context.workspace)).tools
            )
        external = (
            []
            if context.trust_level == "read_only"
            else await app.mcp.tools_for_workspace(context.workspace)
        )
        return tool_catalog([*explicit, *official, *external])

    async def clear_thread_grants(self, thread_id: str) -> dict[str, Any]:
        app = await self._app_for_thread(thread_id)
        await app._save_thread_policy(thread_id, clear_grants=True)
        return {"cleared": True}

    async def hook_status(self, workspace_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        return {
            **app.hooks.status(),
            "hooks": [
                {
                    "event": hook.event,
                    "name": hook.name,
                    "source": hook.source,
                    "blocking": hook.blocking,
                    "timeout": hook.timeout,
                }
                for hook in app.hooks.hooks
            ],
        }

    async def set_hook_trust(self, workspace_id: str, trusted: bool) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        await asyncio.to_thread(app.hooks.trust if trusted else app.hooks.untrust)
        return await self.hook_status(workspace_id)

    async def reload_hooks(self, workspace_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        await asyncio.to_thread(app.hooks.reload)
        return await self.hook_status(workspace_id)

    async def hook_audit(self, workspace_id: str) -> list[dict[str, Any]]:
        app = await self._app_for_workspace(workspace_id)
        rows = await app.audit.list(limit=200)
        return [
            {
                "id": str(row["id"]),
                "at": row.get("at"),
                **(row.get("details") or {}),
            }
            for row in rows
            if row.get("event") == "hook" and isinstance(row.get("details"), dict)
        ]

    async def session_memory_settings(
        self, thread_id: str, patch: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        app = await self._app_for_thread(thread_id)
        return await app.memory.session_settings(thread_id, patch)


__all__ = ["SessionOperations"]
