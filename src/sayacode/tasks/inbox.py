"""父子 Agent 的持久消息邮箱与一次送达状态。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl

from ..agent.events import action_requests
from ..agent.models import _model_error_message
from .records import TaskError

INBOX_NAMESPACE = ("sayacode", "agent_inbox")


@dataclass(slots=True)
class AgentMessage:
    """一条有明确来源和接收线程的 Agent 间消息。"""

    message_id: str
    sender_thread_id: str
    receiver_thread_id: str
    task_id: str
    kind: str
    content: str
    status: str = "pending"
    created_at: str = ""
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentMessage":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})


class TaskInbox:
    """在 LangGraph Store 中保存父子消息，进程内回调只负责唤醒。"""

    def __init__(self, store: Any, *, on_send: Any = None, on_delivered: Any = None) -> None:
        self.store = store
        self.on_send = on_send
        self.on_delivered = on_delivered
        self._queue_locks: dict[str, asyncio.Lock] = {}

    def _queue_lock(self, thread_id: str) -> asyncio.Lock:
        return self._queue_locks.setdefault(thread_id, asyncio.Lock())

    async def _notify(self, message: AgentMessage) -> None:
        if self.on_send is not None:
            result = self.on_send(message)
            if hasattr(result, "__await__"):
                await result

    async def get(self, message_id: str) -> AgentMessage | None:
        item = await self.store.aget(INBOX_NAMESPACE, message_id)
        return AgentMessage.from_dict(dict(item.value)) if item is not None else None

    async def send(
        self,
        *,
        sender_thread_id: str,
        receiver_thread_id: str,
        task_id: str,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        queued: bool = False,
    ) -> AgentMessage:
        """幂等写入一条消息，并在提交后通知进程内接收方。"""
        identity = message_id or f"message-{uuid4().hex}"
        async with self._queue_lock(receiver_thread_id):
            existing = await self.store.aget(INBOX_NAMESPACE, identity)
            if existing is not None:
                return AgentMessage.from_dict(dict(existing.value))
            message = AgentMessage(
                message_id=identity,
                sender_thread_id=sender_thread_id,
                receiver_thread_id=receiver_thread_id,
                task_id=task_id,
                kind=kind,
                content=content,
                status="queued" if queued else "pending",
                metadata=metadata,
            )
            await self.store.aput(INBOX_NAMESPACE, identity, asdict(message), index=False)
        if not queued:
            await self._notify(message)
        return message

    async def queued(self, receiver_thread_id: str) -> list[AgentMessage]:
        """读取尚未送入本轮的用户消息，供输入框旁的队列展示。"""
        items = await self.store.asearch(
            INBOX_NAMESPACE,
            filter={"receiver_thread_id": receiver_thread_id, "status": "queued"},
            limit=500,
        )
        return sorted(
            [AgentMessage.from_dict(dict(item.value)) for item in items],
            key=lambda item: item.created_at,
        )

    async def user_messages(self, receiver_thread_id: str) -> list[AgentMessage]:
        """排队与待送达的用户输入都呈现在输入框旁。"""
        items = await self.store.asearch(
            INBOX_NAMESPACE,
            filter={"receiver_thread_id": receiver_thread_id},
            limit=500,
        )
        return sorted(
            [
                AgentMessage.from_dict(dict(item.value))
                for item in items
                if item.value.get("kind") in {"user_prompt", "user_followup"}
                and item.value.get("status") in {"queued", "pending"}
            ],
            key=lambda item: item.created_at,
        )

    async def _promote_unlocked(self, receiver_thread_id: str, message_id: str) -> AgentMessage:
        item = await self.store.aget(INBOX_NAMESPACE, message_id)
        if item is None:
            raise KeyError(message_id)
        message = AgentMessage.from_dict(dict(item.value))
        if message.receiver_thread_id != receiver_thread_id or message.status != "queued":
            raise ValueError("排队消息已变化，请刷新后重试")
        message.status = "pending"
        await self.store.aput(INBOX_NAMESPACE, message_id, asdict(message), index=False)
        return message

    async def promote(self, receiver_thread_id: str, message_id: str) -> AgentMessage:
        """把同一条排队消息提升为下一模型步骤输入。"""
        async with self._queue_lock(receiver_thread_id):
            message = await self._promote_unlocked(receiver_thread_id, message_id)
        await self._notify(message)
        return message

    async def promote_next(self, receiver_thread_id: str) -> AgentMessage | None:
        async with self._queue_lock(receiver_thread_id):
            # 直接插话先于普通下一轮队列，避免两条消息意外合并或倒序。
            if any(
                item.kind in {"user_prompt", "user_followup"}
                for item in await self.pending(receiver_thread_id)
            ):
                return None
            queued = await self.queued(receiver_thread_id)
            if not queued:
                return None
            message = await self._promote_unlocked(receiver_thread_id, queued[0].message_id)
        await self._notify(message)
        return message

    async def edit_queued(self, receiver_thread_id: str, message_id: str, content: str) -> AgentMessage:
        async with self._queue_lock(receiver_thread_id):
            item = await self.store.aget(INBOX_NAMESPACE, message_id)
            if item is None:
                raise KeyError(message_id)
            message = AgentMessage.from_dict(dict(item.value))
            if message.receiver_thread_id != receiver_thread_id or message.status != "queued":
                raise ValueError("排队消息已变化，请刷新后重试")
            message.content = content
            await self.store.aput(INBOX_NAMESPACE, message_id, asdict(message), index=False)
            return message

    async def remove_queued(self, receiver_thread_id: str, message_id: str) -> None:
        async with self._queue_lock(receiver_thread_id):
            item = await self.store.aget(INBOX_NAMESPACE, message_id)
            if item is None:
                raise KeyError(message_id)
            message = AgentMessage.from_dict(dict(item.value))
            if message.receiver_thread_id != receiver_thread_id or message.status != "queued":
                raise ValueError("排队消息已变化，请刷新后重试")
            await self.store.adelete(INBOX_NAMESPACE, message_id)

    async def pending(self, receiver_thread_id: str) -> list[AgentMessage]:
        """按创建时间返回某线程尚未送达的消息。"""
        items = await self.store.asearch(
            INBOX_NAMESPACE,
            filter={"receiver_thread_id": receiver_thread_id, "status": "pending"},
            limit=500,
        )
        messages = [AgentMessage.from_dict(dict(item.value)) for item in items]
        return sorted(messages, key=lambda item: item.created_at)

    async def pending_all(self) -> list[AgentMessage]:
        """返回全部待投递消息，供进程重启恢复调度。"""
        items = await self.store.asearch(
            INBOX_NAMESPACE, filter={"status": "pending"}, limit=500
        )
        return sorted(
            [AgentMessage.from_dict(dict(item.value)) for item in items],
            key=lambda item: item.created_at,
        )

    async def acknowledge(self, message_ids: list[str]) -> None:
        """把模型已经接收的消息标记为已送达。"""
        for message_id in dict.fromkeys(message_ids):
            item = await self.store.aget(INBOX_NAMESPACE, message_id)
            if item is None or item.value.get("status") == "delivered":
                continue
            value = dict(item.value)
            value["status"] = "delivered"
            value["delivered_at"] = datetime.now(UTC).isoformat()
            await self.store.aput(INBOX_NAMESPACE, message_id, value, index=False)
            if self.on_delivered is not None and value.get("kind") in {
                "user_prompt", "user_followup"
            }:
                result = self.on_delivered(AgentMessage.from_dict(value))
                if hasattr(result, "__await__"):
                    await result

    async def acknowledge_task(self, receiver_thread_id: str, task_id: str) -> None:
        """主动读取任务结果后确认对应的待投递完成消息。"""
        messages = await self.pending(receiver_thread_id)
        await self.acknowledge(
            [message.message_id for message in messages if message.task_id == task_id]
        )


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    """按 UTF-8 字节限制完成消息，保留头部和完整结果查询提示。"""
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    suffix = "\n[结果已截断；使用 task_status 获取完整内容]"
    budget = max(0, limit - len(suffix.encode("utf-8")))
    preview = raw[:budget].decode("utf-8", "ignore")
    return preview + suffix, True


async def on_task_update(app: Any, record: Any) -> None:
    """发布任务状态；每个已结算轮次向直接父线程发送一次结果。"""
    await app._notifications.put(
        {
            "type": f"task.{record.status}",
            "task_id": record.task_id,
            "thread_id": record.thread_id,
            "role": record.role,
            "title": record.title,
            "status": record.status,
            "outcome": record.last_outcome,
            "result": record.result,
            "error": record.error,
        }
    )
    await app.audit.append(
        "task.status",
        thread_id=record.thread_id,
        task_id=record.task_id,
        details=record.to_dict(),
    )
    if (
        record.parent_thread_id is None
        or record.status not in {"idle", "failed", "paused"}
        or record.turn_seq <= 0
    ):
        return
    body = record.result or record.error or record.stopped_reason or "（没有返回正文）"
    preview, truncated = _bounded_text(body, app._task_notice_limit_bytes())
    content = (
        f"子 Agent {record.title}（{record.role} · {record.task_id}）本轮已结算："
        f"{record.last_outcome or record.status}。\n"
        f"最终结果：\n{preview}\n"
        "把它作为执行证据；如影响后续工作，请更新 Todo。"
    )
    await app.task_inbox.send(
        sender_thread_id=record.thread_id,
        receiver_thread_id=record.parent_thread_id,
        task_id=record.task_id,
        kind="subagent_settled",
        content=content,
        metadata={
            "role": record.role,
            "title": record.title,
            "status": record.status,
            "outcome": record.last_outcome,
            "turn_seq": record.turn_seq,
            "truncated": truncated,
            "delivery_state": record.delivery_state,
        },
        message_id=f"settled:{record.task_id}:{record.turn_seq}",
    )


def schedule_wake(app: Any, message: AgentMessage) -> None:
    """安排接收线程处理消息；相同消息只保留一个进程内句柄。"""
    if app._closed or message.message_id in app._wake_runs:
        return
    control = RunControl()
    app._wake_controls[message.message_id] = control
    app._wake_threads[message.message_id] = message.receiver_thread_id
    app._wake_started_at[message.message_id] = datetime.now(UTC).isoformat()
    task = asyncio.create_task(
        wake_receiver(app, message, control), name=f"sayacode-inbox-{message.message_id}"
    )
    app._wake_runs[message.message_id] = task

    def forget(_task: asyncio.Task[None]) -> None:
        app._wake_runs.pop(message.message_id, None)
        app._wake_controls.pop(message.message_id, None)
        app._wake_threads.pop(message.message_id, None)
        app._wake_started_at.pop(message.message_id, None)

    task.add_done_callback(forget)


async def schedule_pending(app: Any, receiver_thread_id: str | None = None) -> None:
    """恢复当前线程或全部线程尚未处理的消息。"""
    messages = (
        await app.task_inbox.pending(receiver_thread_id)
        if receiver_thread_id is not None
        else await app.task_inbox.pending_all()
    )
    for message in messages:
        schedule_wake(app, message)


async def wake_receiver(
    app: Any, message: AgentMessage, control: RunControl | None = None
) -> None:
    """忙碌线程在安全边界接收，空闲父线程启动内部轮次。"""
    task_record = await app._task_by_thread(message.receiver_thread_id)
    if task_record is not None:
        if app.tasks.is_active(task_record.task_id):
            await app.tasks.wait(task_record.task_id)
        root_id = await app._root_thread_id(message.receiver_thread_id)
        async with app._family_lock_for(root_id):
            if not await app.task_inbox.pending(message.receiver_thread_id):
                return
            root = await app.runtime.get_thread(root_id)
            current = await app.tasks.get(task_record.task_id)
            if (
                root is None
                or root.get("auto_wake_suspended") is True
                or root.get("status") in {"stopping", "stopped"}
                or current.status in {"paused", "interrupted", "stopped"}
                or current.auto_wake_suspended
                or current.delivery_state == "cleaned"
            ):
                return
            if not app.tasks.is_active(current.task_id):
                try:
                    await app.tasks.resume(current.task_id, app._task_runner)
                except TaskError as error:
                    if "already running" not in str(error).lower():
                        raise
        return

    thread_id = message.receiver_thread_id
    async with app._thread_lock(thread_id):
        pending = await app.task_inbox.pending(thread_id)
        if not pending or app._closed:
            return
        thread = await app.runtime.get_thread(thread_id)
        if (
            thread is None
            or thread_id in app._stopping_threads
            or thread.get("auto_wake_suspended") is True
            or thread.get("status") in {"stopping", "stopped"}
        ):
            return
        handle, context = await app._context_for_thread(thread_id)
        snapshot = await app.runtime.get_state(handle, thread_id)
        receipts = set(snapshot.values.get("inbox_receipts", [])) if snapshot.values else set()
        received = [item.message_id for item in pending if item.message_id in receipts]
        if received and not snapshot.next:
            await app.task_inbox.acknowledge(received)
            pending = [item for item in pending if item.message_id not in receipts]
        if not pending or snapshot.interrupts:
            return
        user_requested = any(
            item.kind in {"user_prompt", "user_followup"} for item in pending
        )
        spent = app._wake_counts.get(thread_id, 0)
        if not user_requested and spent >= app._max_consecutive_wakes():
            await app._notifications.put(
                {
                    "type": "agent.wake.deferred",
                    "thread_id": thread_id,
                    "task_id": message.task_id,
                    "title": (message.metadata or {}).get("title"),
                    "reason": "automatic wake budget exhausted; next user input will deliver it",
                }
            )
            return
        app._wake_counts[thread_id] = 0 if user_requested else spent + 1
        try:
            run_id = f"wake-{message.message_id}"
            await app._notifications.put(
                {"type": "run.started", "thread_id": thread_id, "run_id": run_id,
                 "source": "inbox"}
            )
            terminal: dict[str, Any] | None = None
            async for event in app._stream_unlocked(
                None,
                session_id=thread_id,
                include_notifications=False,
                control=control,
                internal_trigger=not bool(snapshot.next),
            ):
                await app._notifications.put({**event, "thread_id": thread_id, "run_id": run_id})
                if event.get("type") in {"run.completed", "run.paused", "run.failed", "run.stopped"}:
                    terminal = event
            if terminal is None:
                return
            if terminal["type"] == "run.paused":
                state = await app.runtime.get_state(handle, thread_id)
                public = {
                    "type": "agent.wake.paused",
                    "thread_id": thread_id,
                    "task_id": message.task_id,
                    "title": (message.metadata or {}).get("title"),
                    "action_requests": action_requests(list(state.interrupts)),
                }
            elif terminal["type"] == "run.completed":
                public = {
                    "type": "agent.wake.completed",
                    "thread_id": thread_id,
                    "task_id": message.task_id,
                    "title": (message.metadata or {}).get("title"),
                    "response": terminal.get("response", ""),
                }
            elif terminal["type"] == "run.stopped":
                public = {
                    "type": "agent.wake.stopped",
                    "thread_id": thread_id,
                    "task_id": message.task_id,
                    "reason": control.drain_reason if control is not None else "stopped",
                }
            else:
                public = {
                    "type": "agent.wake.failed",
                    "thread_id": thread_id,
                    "task_id": message.task_id,
                    "error": terminal.get("error") or terminal["type"],
                }
            app._wake_results[message.message_id] = public
            await app._notifications.put(public)
            if terminal["type"] == "run.completed" and thread_id not in app._stopping_threads:
                await app.task_inbox.promote_next(thread_id)
        except GraphDrained:
            return
        except Exception as exc:
            try:
                profile = app._profile()
            except (KeyError, ValueError):
                profile = None
            await app._notifications.put(
                {
                    "type": "agent.wake.failed",
                    "thread_id": thread_id,
                    "task_id": message.task_id,
                    "error": _model_error_message(exc, profile),
                }
            )


__all__ = [
    "AgentMessage",
    "INBOX_NAMESPACE",
    "TaskInbox",
    "on_task_update",
    "schedule_pending",
    "schedule_wake",
]
