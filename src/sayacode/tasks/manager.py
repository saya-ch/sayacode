"""进程内任务句柄与存储元数据，是任务状态机的执行者。

状态机分三段看，创建时是待定，执行中是运行中或正在停止。
执行结束落到终态，完成失败暂停停止都会涨完成序号。
中断是特殊终态，只表示上个进程没收尾，不代表工具没生效。
并发分组规则很简单，本进程只跟踪活跃表，等待只等表内任务。
持久化记录只做查询和恢复依据，从不拿来做等待条件。"""

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
    """进程内跟踪活跃任务，任务元数据持久化到存储。

    做什么，管创建执行停止等待恢复，外加交付查看和清理。
    参数与返回，构造时绑定存储工作树管理和状态回调。
    调用约束，活跃表只在当前进程有效，跨进程靠存储恢复。
    坑点是活跃表和存储可能不一致，以活跃表判断是否在跑。"""

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
        """做什么，建档并起一个后台协程跑任务，初始态是待定。

        参数与返回，入参含父线程角色提示词工作区和运行器，返回新建档案。
        调用约束，只有建造者角色且是代码仓库才建隔离工作树。
        坑点是工作树建好后才落盘，建失败不会留下半截档案。"""
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
        """做什么，在当前进程恢复已存档任务，再起一个后台协程。

        参数与返回，入参是任务编号运行器和可选追问，返回更新后档案。
        调用约束，运行中任务不可重复恢复，已清理的建造者不可恢复。
        坑点是待定运行中和正在停止会被先标为中断，再按中断路径恢复。"""
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
        """后台协程的唯一出口，负责把运行结果翻译成终态。

        分三步走，先置运行中并落盘，再调运行器拿结果，最后按异常定终态。
        正常返回记完成，排空记停止，暂停异常记暂停，取消记中断，其余记失败。
        只有完成失败暂停停止四种终态会涨完成序号，序号是父线程去重的依据。"""
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
        """做什么，请求正在跑的任务优雅停下，不强杀进程。

        参数与返回，入参是任务编号和原因，返回置为正在停止的档案。
        调用约束，停止只是发排空信号，真停要等运行器配合退出。
        坑点是不在活跃表的任务走孤儿标记路径，不会凭空建停止态。"""
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
        """做什么，等单个进程内任务结束，再返回存储中的元数据。

        参数与返回，入参是任务编号，返回最新档案。
        调用约束，不在活跃表就直接读存储，不会空等。
        坑点是等待只看后台协程是否结束，不校验终态是否合法。"""
        active = self._active.get(task_id)
        if active is not None:
            await active[0]
        return await self.get(task_id)

    def active_task_ids(self) -> list[str]:
        """做什么，返回本进程正在跑的任务编号。

        参数与返回，无入参，返回活跃表键列表快照。
        调用约束，只反映本进程，不含已落盘但无人跑的任务。
        坑点是返回的是拷贝，改它不会影响管理器。"""
        return list(self._active)

    async def wait_active(
        self,
        task_ids: list[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> list[TaskRecord]:
        """做什么，等选定的进程内任务，不轮询持久化记录。

        参数与返回，入参是任务编号组和超时秒数，返回每任务最新档案。
        调用约束，分组规则是只等仍在活跃表的，不在表的直接读存储返回。
        坑点是超时不会报错，只返回当前快照，调用方要自己看状态。"""
        selected = task_ids if task_ids is not None else self.active_task_ids()
        tasks = [self._active[task_id][0] for task_id in selected if task_id in self._active]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return [await self.get(task_id) for task_id in selected]

    async def reconcile_orphans(self) -> list[TaskRecord]:
        """做什么，把上个进程遗留的未完成记录标为待确认。

        参数与返回，无入参，返回被标为中断的档案列表。
        调用约束，只改任务元数据，不重跑图，不重复工具效果。
        坑点是用户需显式恢复，恢复前要先看工作树是否脏了。
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
        """把没收尾的档案标为中断，并写下恢复提示。

        只改状态和备注，不碰工作树，不重跑任务。
        中断后工具效果是否落地是未知的，恢复前必须人工检查。"""
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
        """做什么，退出前先发排空信号，再等活跃任务收尾。

        参数与返回，入参是等待秒数，返回收尾时仍活跃的档案。
        调用约束，分两步走，超时还没完的会被取消并等待。
        坑点是取消后状态由执行协程收尾写，这里不直接写终态。"""
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
        """做什么，按编号从存储读回一条任务档案。

        参数与返回，入参是任务编号，返回重建后的档案对象。
        调用约束，编号不存在直接报错，不返回空值。
        坑点是读到的是快照，改完要调更新才会落盘。"""
        item = await self.store.aget(TASK_NAMESPACE, task_id)
        if item is None:
            raise TaskError(f"Unknown task: {task_id}")
        return TaskRecord.from_dict(item.value)

    async def update(self, record: TaskRecord) -> TaskRecord:
        """做什么，落盘产品侧元数据改动，供后续恢复使用。

        参数与返回，入参是改过的档案，返回同一档案。
        调用约束，调用即刷新更新时间并触发状态回调。
        坑点是并发改同一任务会后写覆盖先写，不要两边同时改。"""
        await self._save(record)
        return record

    async def list(self, *, workspace: Path | None = None) -> list[TaskRecord]:
        """做什么，列出存储里的任务档案，默认按更新时间倒序。

        参数与返回，入参是可选工作区过滤，返回档案列表。
        调用约束，给工作区就只留路径完全匹配的，不做子目录模糊匹配。
        坑点是一次最多读五百条，超了需要分页思路另查。"""
        entries = await self.store.asearch(TASK_NAMESPACE, limit=500)
        result = [TaskRecord.from_dict(entry.value) for entry in entries]
        if workspace is not None:
            expected = str(workspace.resolve())
            result = [record for record in result if record.workspace == expected]
        return sorted(result, key=lambda record: record.updated_at, reverse=True)

    def is_active(self, task_id: str) -> bool:
        """做什么，判断该任务是否还在本进程活跃表里。

        参数与返回，入参是任务编号，返回是否在跑。
        调用约束，只看内存表，不读存储，跨进程结果不可用。
        坑点是刚结束的任务会立刻离表，不要拿它判断终态。"""
        return task_id in self._active

    async def delivery(self, task_id: str) -> dict[str, Any]:
        """做什么，查看该任务工作树相对快照的差异，不自动应用。

        参数与返回，入参是任务编号，返回分支状态统计补丁和忽略文件。
        调用约束，只读不写，但会暂存新增文件的占位以便算差异。
        坑点是没有工作树的任务会直接报错，规划类任务无交付。"""
        return self.worktrees.inspect(await self.get(task_id))

    async def apply_delivery(self, task_id: str) -> dict[str, Any]:
        """做什么，把任务工作树差异合到原仓库，只走补丁应用。

        参数与返回，入参是任务编号，返回是否应用成功和差异原文。
        调用约束，运行中和未确认的任务不可应用，已清理的无交付可合。
        坑点是重复应用靠补丁哈希去重，合后工作树再改会拒绝合。"""
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
        """做什么，删掉该任务的隔离工作树并标记为已清理。

        参数与返回，入参是任务编号，返回清理后的档案。
        调用约束，运行中和未确认的任务不可清理，有未合差异不可清理。
        坑点是清理不可逆，清理后该任务永远不可再恢复执行。"""
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
        """刷新更新时间并落盘，顺带触发外部状态回调。

        回调可能是同步也可能是异步，异步会等它做完。
        落盘失败会直接抛错，调用方不要忽略。"""
        record.updated_at = datetime.now(UTC).isoformat()
        await self.store.aput(TASK_NAMESPACE, record.task_id, record.to_store_dict(), index=False)
        if self.on_update is not None:
            result = self.on_update(record)
            if result is not None:
                await result
