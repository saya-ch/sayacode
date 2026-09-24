"""进程内事件扇出；持久真相仍归 checkpoint、Store 和审计。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


class EventHub:
    """为多个浏览器观察者发布同一事件，并短暂补齐断线间隙。"""

    def __init__(self, *, replay_limit: int = 2048, subscriber_limit: int = 512) -> None:
        if replay_limit < 1 or subscriber_limit < 1:
            raise ValueError("事件缓冲容量必须为正数")
        self.instance_id = uuid4().hex
        self._events: deque[dict[str, Any]] = deque(maxlen=replay_limit)
        self._subscribers: dict[int, tuple[str | None, asyncio.Queue[dict[str, Any]]]] = {}
        self._subscriber_limit = subscriber_limit
        self._sequence = 0
        self._next_subscriber = 0
        self._lock = asyncio.Lock()

    @property
    def sequence(self) -> int:
        return self._sequence

    async def publish(
        self,
        *,
        event_type: str,
        workspace_id: str,
        data: Mapping[str, Any],
        thread_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """事件只存一个短时进程内副本，慢订阅者必须重新拉权威快照。"""
        async with self._lock:
            self._sequence += 1
            event = {
                "seq": self._sequence,
                "instance_id": self.instance_id,
                "type": event_type,
                "workspace_id": workspace_id,
                "thread_id": thread_id,
                "task_id": task_id,
                "run_id": run_id,
                "at": datetime.now(UTC).isoformat(),
                "data": dict(data),
            }
            self._events.append(event)
            for workspace, queue in self._subscribers.values():
                if workspace is not None and workspace != workspace_id:
                    continue
                if queue.full():
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(
                        {
                            "seq": self._sequence,
                            "instance_id": self.instance_id,
                            "type": "stream.resync_required",
                            "workspace_id": workspace_id,
                            "data": {},
                        }
                    )
                else:
                    queue.put_nowait(event)
            return event

    async def subscribe(
        self,
        workspace_id: str | None = None,
        after: int | None = None,
        instance_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """先挂订阅再交付缓冲；无法补齐的旧游标明确要求重读快照。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._subscriber_limit)
        async with self._lock:
            self._next_subscriber += 1
            subscriber_id = self._next_subscriber
            replay = (
                [
                    item
                    for item in self._events
                    if (workspace_id is None or item["workspace_id"] == workspace_id)
                    and item["seq"] > after
                ]
                if after is not None
                else []
            )
            earliest = self._events[0]["seq"] if self._events else self._sequence + 1
            gap = (
                (instance_id is not None and instance_id != self.instance_id)
                or (after is not None and (after < earliest - 1 or after > self._sequence))
            )
            self._subscribers[subscriber_id] = (workspace_id, queue)
            current = self._sequence
        try:
            yield {
                "seq": current,
                "instance_id": self.instance_id,
                "type": "stream.ready",
                "workspace_id": workspace_id or "",
                "data": {"replay_available": not gap},
            }
            if gap:
                yield {
                    "seq": current,
                    "instance_id": self.instance_id,
                    "type": "stream.resync_required",
                    "workspace_id": workspace_id or "",
                    "data": {},
                }
            else:
                for item in replay:
                    yield item
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.pop(subscriber_id, None)


__all__ = ["EventHub"]
