"""Thin background-task and Git worktree adapters for SAYACODE.

LangGraph owns task execution state through the task's own thread and
checkpoint.  This module owns only terminal-process task handles and explicit
code-delivery mechanics.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl

TASK_NAMESPACE = ("sayacode", "tasks")


class TaskError(RuntimeError):
    """A task or worktree operation could not be completed safely."""


class TaskPaused(RuntimeError):
    """A worker reached a LangGraph interrupt and needs terminal input."""


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    thread_id: str
    parent_thread_id: str | None
    role: str
    prompt: str
    workspace: str
    write_access: bool
    pending_input: str | None = None
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    worktree_root: str | None = None
    task_workspace: str | None = None
    branch: str | None = None
    snapshot_commit: str | None = None
    stopped_reason: str | None = None
    error: str | None = None
    result: str | None = None
    delivery_state: str = "none"
    applied_patch_sha256: str | None = None
    profile_name: str | None = None
    profile_snapshot: dict[str, Any] | None = None
    mode: str | None = None
    read_only_reason: str | None = None
    unconfirmed_effects: bool = False
    recovery_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a display/audit-safe view of task metadata."""
        data = asdict(self)
        snapshot = data.get("profile_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("api_key"):
            snapshot["api_key"] = "***"
        return data

    def to_store_dict(self) -> dict[str, Any]:
        """Return the private Store payload needed to recreate the task model."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class WorktreeSnapshot:
    root: Path
    workspace: Path
    branch: str
    snapshot_commit: str


def _run_git(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            input=input_bytes,
            capture_output=True,
            env=env,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TaskError(f"Git command failed: {exc}") from exc
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).decode("utf-8", "replace").strip()
        raise TaskError(detail[-4000:] or f"git {' '.join(args)} failed")
    return completed


def _git_text(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return _run_git(cwd, *args, env=env).stdout.decode("utf-8", "replace").strip()


class WorktreeManager:
    """Create isolated writable task worktrees without changing the caller's index."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_git_workspace(workspace: Path) -> bool:
        """Whether this directory can host a worktree from a committed HEAD."""
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            return False
        try:
            return (
                _run_git(workspace, "rev-parse", "--show-toplevel", check=False).returncode == 0
                and _run_git(workspace, "rev-parse", "--verify", "HEAD", check=False).returncode
                == 0
            )
        except TaskError:
            return False

    def create(self, task_id: str, workspace: Path) -> WorktreeSnapshot:
        workspace = workspace.resolve()
        if not self.is_git_workspace(workspace):
            raise TaskError("Writable background tasks require a Git workspace")
        repo_root = Path(_git_text(workspace, "rev-parse", "--show-toplevel")).resolve()
        try:
            relative_workspace = workspace.relative_to(repo_root)
        except ValueError as exc:
            raise TaskError("Workspace must be inside the Git repository") from exc
        target_root = (self.base_dir / task_id).resolve()
        try:
            target_root.relative_to(self.base_dir)
        except ValueError as exc:
            raise TaskError("Worktree path escaped the SAYACODE worktree directory") from exc
        if target_root.exists():
            raise TaskError(f"Task worktree already exists: {target_root}")

        branch = f"sayacode/task-{task_id}"
        snapshot_commit = self._snapshot_commit(repo_root, task_id)
        try:
            _run_git(repo_root, "worktree", "add", "-b", branch, str(target_root), snapshot_commit)
        except Exception:
            _run_git(repo_root, "branch", "-D", branch, check=False)
            raise
        task_workspace = (target_root / relative_workspace).resolve()
        if not task_workspace.is_dir():
            _run_git(repo_root, "worktree", "remove", "--force", str(target_root), check=False)
            _run_git(repo_root, "branch", "-D", branch, check=False)
            raise TaskError("Created worktree did not contain the requested workspace")
        return WorktreeSnapshot(target_root, task_workspace, branch, snapshot_commit)

    def inspect(self, record: TaskRecord) -> dict[str, Any]:
        if not record.worktree_root or not record.snapshot_commit:
            raise TaskError("Task has no writable worktree")
        root = Path(record.worktree_root).resolve()
        self._assert_managed(root)
        self._stage_intent_to_add(root)
        patch = _run_git(
            root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index",
            record.snapshot_commit,
        ).stdout.decode("utf-8", "surrogateescape")
        return {
            "task_id": record.task_id,
            "branch": _git_text(root, "branch", "--show-current"),
            "status": _git_text(root, "status", "--short", "--branch"),
            "stat": _git_text(
                root, "diff", "--no-ext-diff", "--no-textconv", "--stat",
                record.snapshot_commit,
            ),
            "patch": patch,
            "patch_sha256": hashlib.sha256(patch.encode("utf-8", "surrogateescape")).hexdigest(),
            "ignored_paths": self._ignored_untracked(root),
        }

    def apply_delivery(self, record: TaskRecord) -> dict[str, Any]:
        if not record.worktree_root or not record.snapshot_commit:
            raise TaskError("Task has no delivery to apply")
        source_workspace = Path(record.workspace).resolve()
        root = Path(record.worktree_root).resolve()
        self._assert_managed(root)
        repo_root = Path(_git_text(source_workspace, "rev-parse", "--show-toplevel")).resolve()
        delivery = self.inspect(record)
        patch = str(delivery["patch"])
        if not patch.strip():
            return {"applied": False, "reason": "Task has no changes", "delivery": delivery}
        if record.delivery_state == "applied":
            if delivery["patch_sha256"] == record.applied_patch_sha256:
                return {"applied": False, "reason": "Delivery was already applied", "delivery": delivery}
            raise TaskError("Worktree changed after delivery; inspect the new diff before a new task")
        raw = patch.encode("utf-8", "surrogateescape")
        _run_git(repo_root, "apply", "--check", "--binary", "-", input_bytes=raw)
        _run_git(repo_root, "apply", "--binary", "-", input_bytes=raw)
        return {"applied": True, "delivery": delivery}

    def remove(self, record: TaskRecord) -> None:
        if not record.worktree_root:
            return
        root = Path(record.worktree_root).resolve()
        self._assert_managed(root)
        delivery = self.inspect(record)
        patch = str(delivery["patch"])
        if delivery["ignored_paths"]:
            raise TaskError(
                "Ignored files remain in the worktree; move or remove them explicitly before cleanup"
            )
        if patch:
            if record.delivery_state != "applied" or not record.applied_patch_sha256:
                raise TaskError("Unapplied task changes remain; inspect and apply delivery first")
            if delivery["patch_sha256"] != record.applied_patch_sha256:
                raise TaskError("Task changed after delivery was applied; inspect the new diff")
        source_workspace = Path(record.workspace).resolve()
        repo_root = Path(_git_text(source_workspace, "rev-parse", "--show-toplevel")).resolve()
        if patch:
            _run_git(
                repo_root, "apply", "--reverse", "--check", "--binary", "-",
                input_bytes=patch.encode("utf-8", "surrogateescape"),
            )
        _run_git(repo_root, "worktree", "remove", "--force", str(root))

    def _snapshot_commit(self, repo_root: Path, task_id: str) -> str:
        """Build a temporary commit from HEAD plus staged, unstaged, and untracked files."""
        with tempfile.TemporaryDirectory(prefix="sayacode-index-") as temp_dir:
            index = str(Path(temp_dir) / "index")
            env = {
                **os.environ,
                "GIT_INDEX_FILE": index,
                "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "SAYACODE"),
                "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "sayacode@local"),
                "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "SAYACODE"),
                "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "sayacode@local"),
            }
            _run_git(repo_root, "read-tree", "HEAD", env=env)
            diff = _run_git(
                repo_root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"
            ).stdout
            if diff:
                _run_git(repo_root, "apply", "--cached", "--binary", "-", env=env, input_bytes=diff)
            untracked = (
                _run_git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
                .stdout.decode("utf-8", "surrogateescape")
                .split("\0")
            )
            paths = [path for path in untracked if path]
            if paths:
                _run_git(repo_root, "add", "--force", "--", *paths, env=env)
            tree = _git_text(repo_root, "write-tree", env=env)
            parent = _git_text(repo_root, "rev-parse", "HEAD")
            return _git_text(
                repo_root,
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                f"SAYACODE task snapshot {task_id}",
                env=env,
            )

    def _stage_intent_to_add(self, root: Path) -> None:
        names = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
        paths = [item for item in names.decode("utf-8", "surrogateescape").split("\0") if item]
        if paths:
            _run_git(root, "add", "-N", "--", *paths)

    @staticmethod
    def _ignored_untracked(root: Path) -> list[str]:
        names = _run_git(
            root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"
        ).stdout
        return [name for name in names.decode("utf-8", "surrogateescape").split("\0") if name]

    def _assert_managed(self, target: Path) -> None:
        if target.parent != self.base_dir:
            raise TaskError("Refusing to operate outside direct SAYACODE worktrees")


TaskRunner = Callable[[TaskRecord, RunControl], Awaitable[str | None]]


class TaskManager:
    """Track active tasks in-process and persist task metadata in LangGraph Store."""

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
        write_access: bool,
        runner: TaskRunner,
        profile_name: str | None = None,
        profile_snapshot: dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> TaskRecord:
        workspace = workspace.expanduser().resolve()
        read_only_reason: str | None = None
        if role == "builder" and write_access and not self.worktrees.is_git_workspace(workspace):
            write_access = False
            mode = "plan"
            read_only_reason = "No committed Git workspace; builder runs read-only"
        elif role == "builder" and not write_access:
            mode = "plan"
            read_only_reason = "Builder was delegated without write access"
        elif role in {"planner", "reviewer"}:
            if write_access:
                read_only_reason = "Only builders may write"
            write_access = False
            mode = "review" if role == "reviewer" else "plan"
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
            write_access=write_access,
            profile_name=profile_name,
            profile_snapshot=deepcopy(profile_snapshot) if profile_snapshot is not None else None,
            mode=mode,
            read_only_reason=read_only_reason,
        )
        if write_access:
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
        """Resume a checkpointed task in the current CLI process."""
        if task_id in self._active:
            raise TaskError("Task is already running")
        record = await self.get(task_id)
        if record.status in {"pending", "running", "stopping"}:
            record = await self._mark_orphaned(record)
        if record.status not in {"paused", "stopped", "interrupted", "completed", "failed"}:
            raise TaskError(f"Task cannot be resumed from state: {record.status}")
        if record.write_access and record.delivery_state == "cleaned":
            raise TaskError("Cleaned builder worktree cannot be resumed")
        if record.write_access and record.worktree_root:
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
            # A natural graph completion wins even if a drain was requested on
            # the same tick. GraphDrained is the distinct resumable stop path.
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
        """Wait for one in-process task if it is active, then return stored metadata."""
        active = self._active.get(task_id)
        if active is not None:
            await active[0]
        return await self.get(task_id)

    def active_task_ids(self) -> list[str]:
        """Return the currently running task IDs in this CLI process."""
        return list(self._active)

    async def wait_active(
        self, task_ids: list[str] | None = None, *, timeout: float | None = None,
    ) -> list[TaskRecord]:
        """Wait for selected in-process tasks without polling persisted records."""
        selected = task_ids if task_ids is not None else self.active_task_ids()
        tasks = [self._active[task_id][0] for task_id in selected if task_id in self._active]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return [await self.get(task_id) for task_id in selected]

    async def reconcile_orphans(self) -> list[TaskRecord]:
        """Mark unfinished records from a previous process as unconfirmed.

        This only changes task metadata. It never reruns the graph or repeats
        a tool effect; a user must explicitly resume the task afterwards.
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
                "pending", "running", "stopping",
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
        """Persist intentional product metadata changes before a later resume."""
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


__all__ = [
    "TASK_NAMESPACE",
    "TaskError",
    "TaskManager",
    "TaskPaused",
    "TaskRecord",
    "WorktreeManager",
    "WorktreeSnapshot",
]
