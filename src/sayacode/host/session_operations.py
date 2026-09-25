"""会话维护与工作区扩展操作；执行真相仍归官方图和本地服务。"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, cast

from langchain.agents.middleware import FilesystemFileSearchMiddleware, TodoListMiddleware
from langchain_core.runnables import RunnableConfig

from ..application import SayacodeApp
from ..sessions import _workspace_key
from ..tasks.inbox import INBOX_NAMESPACE
from ..tasks.manager import TASK_NAMESPACE
from ..tasks.records import TaskRecord
from ..tools import tool_catalog
from .events import EventHub
from .workspaces import browse_directory, workspace_id


class SessionOperations:
    """Web 宿主注入工作区和线程查找方法后即可复用的类型化操作。"""

    events: EventHub

    async def _app_for_workspace(self, workspace_id: str) -> SayacodeApp:
        raise NotImplementedError

    async def _app_for_thread(self, thread_id: str) -> SayacodeApp:
        raise NotImplementedError

    async def _publish_thread_change(
        self, thread_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    def _session_deletion_guard(self, thread_id: str) -> asyncio.Lock:
        """与新运行调度共用互斥锁。"""
        raise NotImplementedError

    def _active_session_threads(self, thread_ids: set[str]) -> set[str]:
        """查询主运行、通知唤醒和子任务的进程内活动状态。"""
        raise NotImplementedError

    def _set_session_deleting(self, thread_id: str, deleting: bool) -> None:
        """阻止删除期间再派发这个会话树的任务。"""
        raise NotImplementedError

    async def _remove_thread_attachments(self, thread_id: str) -> None:
        """只清理本产品拥有的附件文件，未知内容保留并上报。"""
        raise NotImplementedError

    async def browse_directories(self, path: str | None = None) -> dict[str, Any]:
        return await browse_directory(path)

    async def _session_descendants(self, app: SayacodeApp, thread_id: str) -> list[TaskRecord]:
        descendants: list[TaskRecord] = []
        pending = deque([thread_id])
        visited = {thread_id}
        while pending:
            parent = pending.popleft()
            offset = 0
            while True:
                page = await app.runtime.store.asearch(
                    TASK_NAMESPACE,
                    filter={"parent_thread_id": parent},
                    limit=100,
                    offset=offset,
                )
                for item in page:
                    record = TaskRecord.from_dict(dict(item.value))
                    if record.thread_id in visited:
                        continue
                    visited.add(record.thread_id)
                    descendants.append(record)
                    pending.append(record.thread_id)
                if len(page) < 100:
                    break
                offset += len(page)
        return descendants

    async def session_deletion_preview(self, thread_id: str) -> dict[str, Any]:
        app = await self._app_for_thread(thread_id)
        row = await app.runtime.get_thread(thread_id)
        if row is None:
            raise KeyError(f"未知会话：{thread_id}")
        if row.get("is_background"):
            raise ValueError("请从子任务面板管理子 Agent，不能当作主会话删除")
        descendants = await self._session_descendants(app, thread_id)
        ids = {thread_id, *(item.thread_id for item in descendants)}
        blockers: list[str] = []
        warnings: list[str] = []
        active = self._active_session_threads(ids)
        if active:
            blockers.append("会话或子 Agent 正在运行；请先停止整个会话树并等待停止完成")
        if any(item.status in {"pending", "running", "stopping"} for item in descendants):
            blockers.append("仍有未停止的子 Agent；请等待状态更新后再删除")
        undelivered = [item for item in descendants if item.worktree_root]
        if undelivered:
            blockers.append(
                "仍有子 Agent 工作树；请先检查、应用或保留交付，并在任务面板显式清理工作树："
                + "、".join(item.title or item.task_id for item in undelivered)
            )
        if any(item.unconfirmed_effects for item in descendants):
            warnings.append("子 Agent 曾强制中断，部分外部操作可能未确认；删除不会撤销这些操作")
        return {
            "thread_id": thread_id,
            "allowed": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "child_task_count": len(descendants),
            "keeps_audit": True,
            "keeps_long_term_memory": True,
            "keeps_workspace_files": True,
        }

    async def delete_session(self, thread_id: str) -> dict[str, Any]:
        """删除主会话树的原生检查点和产品目录，保留审计、记忆与工作区文件。"""
        async with self._session_deletion_guard(thread_id):
            self._set_session_deleting(thread_id, True)
            try:
                preview = await self.session_deletion_preview(thread_id)
                if preview["blockers"]:
                    raise RuntimeError("；".join(preview["blockers"]))
                app = await self._app_for_thread(thread_id)
                descendants = await self._session_descendants(app, thread_id)
                ids = {thread_id, *(item.thread_id for item in descendants)}
                identity = workspace_id(app.workspace)
                active_key = _workspace_key(app.workspace)
                active = await app.runtime.store.aget(("active_sessions",), active_key)
                active_id = str(active.value.get("thread_id") or "") if active else ""

                # 先选择或创建接替会话，避免活动指针指向已删除的检查点。
                next_session_id = active_id or None
                if active_id == thread_id or app.session_id == thread_id:
                    rows = await app.runtime.list_threads(workspace=app.workspace, limit=1000)
                    alternatives = [
                        row for row in rows
                        if not row.get("is_background") and row.get("thread_id") != thread_id
                    ]
                    if alternatives:
                        chosen = max(alternatives, key=lambda row: str(row.get("updated_at") or ""))
                        next_session_id = str(chosen["thread_id"])
                        await app._set_active_session(next_session_id)
                        app.session_id = next_session_id
                    else:
                        next_session_id = await app._new_session()

                # 邮箱中的已送达与未送达消息都不能再引用被删线程。
                offset = 0
                inbox_keys: list[str] = []
                while True:
                    page = await app.runtime.store.asearch(INBOX_NAMESPACE, limit=100, offset=offset)
                    inbox_keys.extend(
                        item.key for item in page
                        if item.value.get("sender_thread_id") in ids
                        or item.value.get("receiver_thread_id") in ids
                    )
                    if len(page) < 100:
                        break
                    offset += len(page)
                for key in inbox_keys:
                    await app.runtime.store.adelete(INBOX_NAMESPACE, key)

                for item in reversed(descendants):
                    await app.runtime.checkpointer.adelete_thread(item.thread_id)
                    await app.runtime.store.adelete(("threads",), item.thread_id)
                    await app.runtime.store.adelete(TASK_NAMESPACE, item.task_id)
                    app._thread_policies.pop(item.thread_id, None)
                    app._wake_counts.pop(item.thread_id, None)
                await app.runtime.checkpointer.adelete_thread(thread_id)
                await app.runtime.store.adelete(("threads",), thread_id)
                app._thread_policies.pop(thread_id, None)
                app._wake_counts.pop(thread_id, None)
                attachment_warnings: list[str] = []
                for removed_thread in ids:
                    try:
                        await self._remove_thread_attachments(removed_thread)
                    except (OSError, RuntimeError, PermissionError) as error:
                        attachment_warnings.append(
                            f"{removed_thread} 的附件未能完全清理：{error}"
                        )
                await self.events.publish(
                    event_type="session.deleted",
                    workspace_id=identity,
                    thread_id=thread_id,
                    data={"next_session_id": next_session_id},
                )
                return {
                    "deleted": True,
                    "thread_id": thread_id,
                    "next_session_id": next_session_id,
                    "warnings": attachment_warnings,
                }
            finally:
                self._set_session_deleting(thread_id, False)

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
