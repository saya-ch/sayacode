"""Safety and restart behavior for independent local tasks."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from sayacode.runtime import AgentRuntime
from sayacode.tasks import (
    TASK_NAMESPACE,
    TaskError,
    TaskManager,
    TaskRecord,
    WorktreeManager,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Probe")
    git(root, "config", "user.email", "probe@example.invalid")
    (root / "sample.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "sample.txt")
    git(root, "commit", "-m", "base")
    return root


def builder_record(root: Path, manager: WorktreeManager, task_id: str) -> TaskRecord:
    snapshot = manager.create(task_id, root)
    return TaskRecord(
        task_id=task_id,
        thread_id=f"task-{task_id}",
        parent_thread_id="session-parent",
        role="builder",
        prompt="edit",
        workspace=str(root),
        worktree_enabled=True,
        worktree_root=str(snapshot.root),
        task_workspace=str(snapshot.workspace),
        branch=snapshot.branch,
        snapshot_commit=snapshot.snapshot_commit,
    )


def test_cleanup_preserves_unapplied_or_changed_worktree(tmp_path: Path) -> None:
    root = repository(tmp_path)
    manager = WorktreeManager(tmp_path / "worktrees")
    record = builder_record(root, manager, "cleanup")
    child = Path(record.task_workspace or "")
    (child / "sample.txt").write_text("child edit\n", encoding="utf-8")

    with pytest.raises(TaskError, match="Unapplied"):
        manager.remove(record)
    assert (child / "sample.txt").read_text(encoding="utf-8") == "child edit\n"

    delivery = manager.apply_delivery(record)
    record.delivery_state = "applied"
    record.applied_patch_sha256 = delivery["delivery"]["patch_sha256"]
    assert manager.apply_delivery(record)["reason"] == "Delivery was already applied"
    (child / "sample.txt").write_text("changed after apply\n", encoding="utf-8")
    with pytest.raises(TaskError, match="changed after delivery"):
        manager.remove(record)
    assert child.exists()

    (child / "sample.txt").write_text("child edit\n", encoding="utf-8")
    manager.remove(record)
    assert not child.exists()
    assert (root / "sample.txt").read_text(encoding="utf-8") == "child edit\n"


def test_git_external_diff_is_not_used_for_snapshot_or_delivery(tmp_path: Path) -> None:
    root = repository(tmp_path)
    git(root, "config", "diff.external", "this-program-must-not-be-run")
    (root / "sample.txt").write_text("dirty before spawn\n", encoding="utf-8")
    manager = WorktreeManager(tmp_path / "worktrees")
    record = builder_record(root, manager, "no-external-diff")
    child = Path(record.task_workspace or "")
    assert (child / "sample.txt").read_text(encoding="utf-8") == "dirty before spawn\n"
    (child / "sample.txt").write_text("task edit\n", encoding="utf-8")
    delivery = manager.inspect(record)
    assert "task edit" in delivery["patch"]


def test_cleanup_preserves_ignored_files(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "ignore build output")
    manager = WorktreeManager(tmp_path / "worktrees")
    record = builder_record(root, manager, "ignored")
    ignored = Path(record.task_workspace or "") / "build" / "result.bin"
    ignored.parent.mkdir()
    ignored.write_bytes(b"unapplied-result")
    with pytest.raises(TaskError, match="Ignored files remain"):
        manager.remove(record)
    assert ignored.read_bytes() == b"unapplied-result"


@pytest.mark.asyncio
async def test_running_builder_cannot_be_applied(tmp_path: Path) -> None:
    root = repository(tmp_path)
    release = asyncio.Event()

    async def runner(_record: TaskRecord, _control: object) -> str:
        await release.wait()
        return "done"

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        tasks = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))
        record = await tasks.spawn(
            parent_thread_id="session", role="builder", prompt="edit", workspace=root,
            worktree_enabled=True, runner=runner, profile_name="profile-a", trust_level="ask",
        )
        assert tasks.active_task_ids() == [record.task_id]
        assert record.profile_name == "profile-a" and record.trust_level == "ask"
        with pytest.raises(TaskError, match="Stop or wait"):
            await tasks.apply_delivery(record.task_id)
        release.set()
        done = await tasks.wait_active()
        assert done[0].status == "completed"


@pytest.mark.asyncio
async def test_orphans_are_unconfirmed_and_pending_prompt_is_preserved(tmp_path: Path) -> None:
    seen: list[str | None] = []

    async def runner(record: TaskRecord, _control: object) -> str:
        seen.append(record.pending_input)
        return "done"

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        tasks = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))
        for status in ("pending", "running"):
            record = TaskRecord(
                task_id=status, thread_id=f"task-{status}", parent_thread_id=None,
                role="planner", prompt="initial", workspace=str(tmp_path),
                worktree_enabled=False, status=status,
                pending_input="initial" if status == "pending" else None,
            )
            await runtime.store.aput(TASK_NAMESPACE, status, record.to_dict(), index=False)
        recovered = await tasks.reconcile_orphans()
        assert {item.task_id for item in recovered} == {"pending", "running"}
        assert all(item.status == "interrupted" and item.unconfirmed_effects for item in recovered)
        await tasks.resume("pending", runner)
        await tasks.resume("running", runner)
        await tasks.wait_active()
        assert seen == ["initial", None]
        assert (await tasks.get("pending")).unconfirmed_effects


@pytest.mark.asyncio
async def test_non_git_builder_uses_shared_workspace_and_keeps_profile_private(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    seen: list[tuple[str | None, bool]] = []

    async def runner(record: TaskRecord, _control: object) -> str:
        seen.append((record.trust_level, record.worktree_enabled))
        return "done"

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        tasks = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))
        assert not tasks.worktrees.is_git_workspace(workspace)
        record = await tasks.spawn(
            parent_thread_id="session", role="builder", prompt="edit", workspace=workspace,
            worktree_enabled=True, runner=runner, profile_name="temporary",
            profile_snapshot={"name": "temporary", "model": "fake", "api_key": "private-key"},
            trust_level="ask",
        )
        assert record.role == "builder"
        assert record.trust_level == "ask" and record.worktree_enabled is False
        assert record.worktree_root is None
        assert record.to_dict()["profile_snapshot"]["api_key"] == "***"
        assert record.to_store_dict()["profile_snapshot"]["api_key"] == "private-key"
        await tasks.wait(record.task_id)
        assert seen == [("ask", False)]
        assert (await tasks.get(record.task_id)).profile_snapshot["api_key"] == "private-key"
