"""进程内任务句柄与 LangGraph Store 元数据。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl

from .records import TaskError, TaskPaused, TaskRecord
from .worktree import WorktreeManager

TASK_NAMESPACE = ("sayacode", "tasks")
TaskRunner = Callable[[TaskRecord, RunControl], Awaitable[str | None]]


class TaskManager:
    """进程内跟踪活跃任务。任务元数据持久化到存储。"""

    def __init__(
        self,
        store: Any,
        worktrees: WorktreeManager,
        *,
        on_update: Callable[[TaskRecord], Awaitable[None] | None] | None = None,
    ) -> None:
        self.store = store
        self.worktrees = worktrees
        self.on_update = on_update
        self._active: dict[str, tuple[asyncio.Task[None], RunControl]] = {}

    async def spawn(
        self,
        *,
        parent_thread_id: str | None,
        role: str,
        prompt: str,
        workspace: Path,
        worktree_enabled: bool,
        runner: TaskRunner,
        profile_name: str | None = None,
        profile_snapshot: dict[str, Any] | None = None,
        trust_level: str = "ask",
    ) -> TaskRecord:
        workspace = workspace.expanduser().resolve()
        worktree_enabled = (
            role == "builder" and worktree_enabled and self.worktrees.is_git_workspace(workspace)
        )
        task_id = uuid4().hex[:12]
        thread_id = f"task-{task_id}"
        record = TaskRecord(
            task_id=task_id,
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            role=role,
            prompt=prompt,
            pending_input=prompt,
            workspace=str(workspace),
            worktree_enabled=worktree_enabled,
            profile_name=profile_name,
            profile_snapshot=deepcopy(profile_snapshot) if profile_snapshot is not None else None,
            trust_level=trust_level,
        )
        if worktree_enabled:
            snapshot = self.worktrees.create(task_id, workspace)
            record.worktree_root = str(snapshot.root)
            record.task_workspace = str(snapshot.workspace)
            record.branch = snapshot.branch
            record.snapshot_commit = snapshot.snapshot_commit
        await self._save(record)
        control = RunControl()
        task = asyncio.create_task(
            self._execute(record, control, runner), name=f"sayacode-{task_id}"
        )
        self._active[task_id] = (task, control)
        return record

    async def resume(
        self, task_id: str, runner: TaskRunner, *, prompt: str | None = None
    ) -> TaskRecord:
        """在当前进程恢复已存档任务。"""
        if task_id in self._active:
            raise TaskError("Task is already running")
        record = await self.get(task_id)
        if record.status in {"pending", "running", "stopping"}:
            record = await self._mark_orphaned(record)
        if record.status not in {"paused", "stopped", "interrupted", "completed", "failed"}:
            raise TaskError(f"Task cannot be resumed from state: {record.status}")
        if record.worktree_enabled and record.delivery_state == "cleaned":
            raise TaskError("Cleaned builder worktree cannot be resumed")
        if record.worktree_enabled and record.worktree_root:
            root = Path(record.worktree_root).resolve()
            self.worktrees._assert_managed(root)
            if not root.is_dir():
                raise TaskError("Builder worktree is missing; its task cannot be resumed")
        if prompt is not None:
            record.pending_input = prompt
        await self._save(record)
        control = RunControl()
        task = asyncio.create_task(
            self._execute(record, control, runner), name=f"sayacode-{task_id}"
        )
        self._active[task_id] = (task, control)
        return record

    async def _execute(self, record: TaskRecord, control: RunControl, runner: TaskRunner) -> None:
        record.status = "running"
        await self._save(record)
        try:
            record.result = await runner(record, control)
            # 同一节拍里自然完成优先。排空是另一条可恢复停止路径。
            record.status = "completed"
            record.stopped_reason = None
        except GraphDrained:
            record.status = "stopped"
            record.stopped_reason = control.drain_reason or "graph drained"
        except TaskPaused as paused:
            record.status = "paused"
            record.result = str(paused) or None
        except asyncio.CancelledError:
            record.status = "interrupted"
            record.stopped_reason = "forced cancellation"
            raise
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
        finally:
            if record.status in {"completed", "failed", "paused", "stopped"}:
                record.completion_seq += 1
            await self._save(record)
            self._active.pop(record.task_id, None)

    async def stop(self, task_id: str, reason: str = "user requested stop") -> TaskRecord:
        record = await self.get(task_id)
        active = self._active.get(task_id)
        if active is None:
            if record.status in {"pending", "running", "stopping"}:
                return await self._mark_orphaned(record)
            return record
        record.status = "stopping"
        await self._save(record)
        active[1].request_drain(reason)
        return record

    async def wait(self, task_id: str) -> TaskRecord:
        """等单个进程内任务结束。再返回存储中的元数据。"""
        active = self._active.get(task_id)
        if active is not None:
            await active[0]
        return await self.get(task_id)

    def active_task_ids(self) -> list[str]:
        """返回本进程正在跑的任务编号。"""
        return list(self._active)

    async def wait_active(
        self,
        task_ids: list[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> list[TaskRecord]:
        """等选定的进程内任务。不轮询持久化记录。"""
        selected = task_ids if task_ids is not None else self.active_task_ids()
        tasks = [self._active[task_id][0] for task_id in selected if task_id in self._active]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return [await self.get(task_id) for task_id in selected]

    async def reconcile_orphans(self) -> list[TaskRecord]:
        """把上个进程遗留的未完成记录标为待确认。

        只改任务元数据。不重跑图。不重复工具效果。用户需显式恢复。
        """
        records: list[TaskRecord] = []
        offset = 0
        while True:
            items = await self.store.asearch(TASK_NAMESPACE, limit=100, offset=offset)
            if not items:
                break
            records.extend(TaskRecord.from_dict(item.value) for item in items)
            offset += len(items)
        recovered: list[TaskRecord] = []
        for record in records:
            if record.task_id not in self._active and record.status in {
                "pending",
                "running",
                "stopping",
            }:
                recovered.append(await self._mark_orphaned(record))
        return recovered

    async def _mark_orphaned(self, record: TaskRecord) -> TaskRecord:
        record.status = "interrupted"
        record.unconfirmed_effects = True
        record.recovery_note = (
            "Previous CLI process ended before completion. Effects after the last "
            "checkpoint are unconfirmed; inspect the task worktree before resuming."
        )
        record.stopped_reason = "previous CLI process ended"
        await self._save(record)
        return record

    async def shutdown(self, timeout: float = 10.0) -> list[TaskRecord]:
        active = list(self._active.items())
        for _, (_, control) in active:
            control.request_drain("CLI exit")
        if active:
            tasks = [pair[0] for _, pair in active]
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return [await self.get(task_id) for task_id, _ in active]

    async def get(self, task_id: str) -> TaskRecord:
        item = await self.store.aget(TASK_NAMESPACE, task_id)
        if item is None:
            raise TaskError(f"Unknown task: {task_id}")
        return TaskRecord.from_dict(item.value)

    async def update(self, record: TaskRecord) -> TaskRecord:
        """落盘产品侧元数据改动。供后续恢复使用。"""
        await self._save(record)
        return record

    async def list(self, *, workspace: Path | None = None) -> list[TaskRecord]:
        entries = await self.store.asearch(TASK_NAMESPACE, limit=500)
        result = [TaskRecord.from_dict(entry.value) for entry in entries]
        if workspace is not None:
            expected = str(workspace.resolve())
            result = [record for record in result if record.workspace == expected]
        return sorted(result, key=lambda record: record.updated_at, reverse=True)

    def is_active(self, task_id: str) -> bool:
        return task_id in self._active

    async def delivery(self, task_id: str) -> dict[str, Any]:
        return self.worktrees.inspect(await self.get(task_id))

    async def apply_delivery(self, task_id: str) -> dict[str, Any]:
        if task_id in self._active:
            raise TaskError("Stop or wait for the builder before applying its delivery")
        record = await self.get(task_id)
        if record.status in {"pending", "running", "stopping"}:
            raise TaskError("Task completion is unconfirmed; reconcile it before delivery")
        if record.delivery_state == "cleaned":
            raise TaskError("Cleaned worktree has no delivery to apply")
        result = self.worktrees.apply_delivery(record)
        if result.get("applied"):
            record.delivery_state = "applied"
            record.applied_patch_sha256 = str(result["delivery"]["patch_sha256"])
            await self._save(record)
        return result

    async def remove_worktree(self, task_id: str) -> TaskRecord:
        record = await self.get(task_id)
        if task_id in self._active:
            raise TaskError("Stop a running task before removing its worktree")
        if record.status in {"pending", "running", "stopping"}:
            raise TaskError("Task completion is unconfirmed; reconcile it before cleanup")
        self.worktrees.remove(record)
        record.delivery_state = "cleaned"
        record.task_workspace = None
        record.worktree_root = None
        await self._save(record)
        return record

    async def _save(self, record: TaskRecord) -> None:
        record.updated_at = datetime.now(UTC).isoformat()
        await self.store.aput(TASK_NAMESPACE, record.task_id, record.to_store_dict(), index=False)
        if self.on_update is not None:
            result = self.on_update(record)
            if result is not None:
                await result
