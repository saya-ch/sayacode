"""Web 会话的排队消息和整棵 Agent 任务树停止操作。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from ..agent.runtime import AgentRuntime
from ..application import SayacodeApp
from ..config import Profile
from ..tasks import TaskManager, TaskRecord
from ..tasks.inbox import AgentMessage
from .attachments import AttachmentStore
from .events import EventHub


def _queued_view(message: AgentMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "thread_id": message.receiver_thread_id,
        "text": message.content,
        "status": message.status,
        "created_at": message.created_at,
        "attachments": (message.metadata or {}).get("attachments", []),
    }


class RunActions:
    """只组织产品动作；实际 Agent 执行仍由 LangGraph 图承担。"""

    events: EventHub
    runtime: AgentRuntime
    tasks: TaskManager
    attachments: AttachmentStore
    _deleting_sessions: set[str]
    _stopping_sessions: set[str]
    _stopping_families: dict[str, set[str]]
    _runs: dict[str, Any]

    async def _thread_ref(
        self, thread_id: str
    ) -> tuple[SayacodeApp, dict[str, Any], str, TaskRecord | None]:
        raise NotImplementedError

    def _session_deletion_guard(self, thread_id: str) -> asyncio.Lock:
        raise NotImplementedError

    async def _family_guard_for(self, thread_id: str) -> asyncio.Lock:
        raise NotImplementedError

    def _active_session_threads(self, thread_ids: set[str]) -> set[str]:
        raise NotImplementedError

    async def _session_descendants(
        self, app: SayacodeApp, thread_id: str
    ) -> list[TaskRecord]:
        raise NotImplementedError

    async def queued_messages(self, thread_id: str) -> list[dict[str, Any]]:
        app, _, _, _ = await self._thread_ref(thread_id)
        return [_queued_view(item) for item in await app.task_inbox.user_messages(thread_id)]

    async def queue_message(
        self,
        thread_id: str,
        text: str,
        *,
        attachment_ids: list[str] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        selected = text.strip()
        ids = attachment_ids or []
        if not selected and not ids:
            raise ValueError("消息不能为空")
        async with await self._family_guard_for(thread_id):
            app, row, identity, record = await self._thread_ref(thread_id)
            if thread_id in self._deleting_sessions:
                raise ValueError("会话正在删除")
            root_id = await app._root_thread_id(thread_id)
            if root_id in self._deleting_sessions:
                raise ValueError("所属主会话正在删除")
            inherited = (
                Profile.from_dict(record.profile_snapshot)
                if record is not None and record.profile_snapshot is not None
                else None
            )
            await app._effective_profile(thread_id, inherited)
            identity_id = message_id or f"user-{uuid4().hex}"
            existing = await app.task_inbox.get(identity_id)
            if existing is not None:
                old_ids = [
                    str(item.get("id"))
                    for item in (existing.metadata or {}).get("attachments", [])
                    if isinstance(item, dict)
                ]
                if (
                    existing.receiver_thread_id != thread_id
                    or existing.kind != "user_prompt"
                    or existing.content != selected
                    or old_ids != ids
                ):
                    raise ValueError("消息标识已用于另一条输入")
                return _queued_view(existing)
            blocked = (
                await app.hooks.trigger(
                    "UserPromptSubmit", {"prompt": selected, "thread_id": thread_id}
                )
                if await app._hooks_enabled(thread_id)
                else None
            )
            if blocked:
                raise ValueError(blocked)
            attachments = await self.attachments.bind(thread_id, ids, identity_id)
            try:
                message = await app.task_inbox.send(
                    sender_thread_id=thread_id,
                    receiver_thread_id=thread_id,
                    task_id=record.task_id if record else "",
                    kind="user_prompt",
                    content=selected,
                    metadata={
                        "attachments": [item.public() for item in attachments],
                        "source": "user",
                    },
                    message_id=identity_id,
                    queued=True,
                )
            except BaseException:
                if ids:
                    await self.attachments.release_bindings(thread_id, ids, identity_id)
                raise
            if message.status != "queued":
                return _queued_view(message)
            await self.events.publish(
                event_type="message.queued",
                workspace_id=identity,
                thread_id=thread_id,
                task_id=record.task_id if record else None,
                data=_queued_view(message),
            )
            if (
                not self._active_session_threads({thread_id})
                and row.get("status") not in {"stopping", "stopped", "paused"}
                and row.get("auto_wake_suspended") is not True
                and (
                    record is None
                    or (
                        record.status not in {"paused", "interrupted", "stopped", "stopping"}
                        and not record.auto_wake_suspended
                    )
                )
                and root_id not in self._stopping_sessions
            ):
                promoted = await app.task_inbox.promote_next(thread_id)
                if promoted is not None and promoted.message_id == message.message_id:
                    message = promoted
            return _queued_view(message)

    async def promote_queued_message(
        self, thread_id: str, message_id: str
    ) -> dict[str, Any]:
        async with await self._family_guard_for(thread_id):
            app, row, identity, record = await self._thread_ref(thread_id)
            root_id = await app._root_thread_id(thread_id)
            root = await self.runtime.get_thread(root_id)
            if record is not None and record.status == "paused":
                raise ValueError("子 Agent 正在等待审批，请先处理审批")
            if record is not None and record.status in {"interrupted", "stopped", "stopping"}:
                raise ValueError("子 Agent 已停止，请先恢复")
            if row.get("status") in {"stopping", "stopped", "paused"} or row.get(
                "auto_wake_suspended"
            ) is True or (record is not None and record.auto_wake_suspended) or (
                root is not None and root.get("auto_wake_suspended") is True
            ):
                raise ValueError("当前线程已暂停或停止，请先恢复")
            message = await app.task_inbox.promote(thread_id, message_id)
            await self.events.publish(
                event_type="message.steered",
                workspace_id=identity,
                thread_id=thread_id,
                task_id=record.task_id if record else None,
                data={"message_id": message_id},
            )
            return _queued_view(message)

    async def edit_queued_message(
        self, thread_id: str, message_id: str, text: str
    ) -> dict[str, Any]:
        selected = text.strip()
        if not selected:
            raise ValueError("消息不能为空")
        async with await self._family_guard_for(thread_id):
            app, _, identity, record = await self._thread_ref(thread_id)
            message = await app.task_inbox.edit_queued(thread_id, message_id, selected)
            await self.events.publish(
                event_type="message.queue_updated",
                workspace_id=identity,
                thread_id=thread_id,
                task_id=record.task_id if record else None,
                data=_queued_view(message),
            )
            return _queued_view(message)

    async def remove_queued_message(
        self, thread_id: str, message_id: str
    ) -> None:
        async with await self._family_guard_for(thread_id):
            app, _, identity, record = await self._thread_ref(thread_id)
            queued = next(
                (item for item in await app.task_inbox.queued(thread_id)
                 if item.message_id == message_id),
                None,
            )
            if queued is None:
                raise ValueError("排队消息已变化，请刷新后重试")
            attachment_ids = [
                str(item["id"]) for item in (queued.metadata or {}).get("attachments", [])
                if isinstance(item, dict) and item.get("id")
            ]
            await app.task_inbox.remove_queued(thread_id, message_id)
            if attachment_ids:
                await self.attachments.release_bindings(thread_id, attachment_ids, message_id)
                for attachment_id in attachment_ids:
                    await self.attachments.discard(thread_id, attachment_id)
            await self.events.publish(
                event_type="message.queue_removed",
                workspace_id=identity,
                thread_id=thread_id,
                task_id=record.task_id if record else None,
                data={"message_id": message_id},
            )

    async def stop_session_tree(self, thread_id: str) -> dict[str, Any]:
        """先封住新任务和自动唤醒，再排空主运行与所有后代任务。"""
        async with self._session_deletion_guard(thread_id):
            app, row, identity, record = await self._thread_ref(thread_id)
            if record is not None or row.get("is_background"):
                raise ValueError("请在所属主会话停止整棵任务树")
            self._stopping_sessions.add(thread_id)
            app._stopping_threads.add(thread_id)
            await self.runtime.update_thread(
                thread_id, {"status": "stopping", "auto_wake_suspended": True}
            )
            descendants = await self._session_descendants(app, thread_id)
            family = {thread_id, *(item.thread_id for item in descendants)}
            self._stopping_families[thread_id] = family
            app._stopping_threads.update(family)
            for child in descendants:
                child.auto_wake_suspended = True
                if self.tasks.is_active(child.task_id) or child.status == "idle":
                    await self.tasks.stop(child.task_id, "parent session stopped")
                else:
                    await self.tasks.update(child)
            run = self._runs.get(thread_id)
            if run is not None:
                run.control.request_drain("user stopped the session tree")
            for message_id, receiver in list(app._wake_threads.items()):
                if receiver in family:
                    control = app._wake_controls.get(message_id)
                    if control is not None:
                        control.request_drain("parent session stopped")
            if run is None and not any(
                receiver == thread_id for receiver in app._wake_threads.values()
            ):
                await self.runtime.set_thread_status(thread_id, "stopped")
            await self.events.publish(
                event_type="run.stopping",
                workspace_id=identity,
                thread_id=thread_id,
                data={"child_tasks": len(descendants)},
            )
            return {"thread_id": thread_id, "status": "stopping", "child_tasks": len(descendants)}


__all__ = ["RunActions"]
