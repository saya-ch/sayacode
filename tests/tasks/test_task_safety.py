"""Safety and restart behavior for independent local tasks."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphDrained

from sayacode.agent import AgentRuntime
from sayacode.tasks import (
    TASK_NAMESPACE,
    TaskError,
    TaskManager,
    TaskRecord,
    WorktreeManager,
)
from sayacode.tasks.inbox import INBOX_NAMESPACE, TaskInbox, on_task_update
from sayacode.tasks.manager import run_task

if TYPE_CHECKING:
    from sayacode.application import SayacodeApp


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
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
            parent_thread_id="session",
            role="builder",
            prompt="edit",
            workspace=root,
            worktree_enabled=True,
            runner=runner,
            profile_name="profile-a",
            trust_level="ask",
        )
        assert tasks.active_task_ids() == [record.task_id]
        assert record.profile_name == "profile-a" and record.trust_level == "ask"
        with pytest.raises(TaskError, match="Stop or wait"):
            await tasks.apply_delivery(record.task_id)
        release.set()
        done = await tasks.wait_active()
        assert done[0].status == "idle"


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
                task_id=status,
                thread_id=f"task-{status}",
                parent_thread_id=None,
                role="planner",
                prompt="initial",
                workspace=str(tmp_path),
                worktree_enabled=False,
                status=status,
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
@pytest.mark.parametrize("failed_status", ["running", "idle"])
async def test_store_failure_releases_task_handle(tmp_path: Path, failed_status: str) -> None:
    class FailingStore:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        async def aput(
            self, namespace: Any, key: str, value: dict[str, Any], **kwargs: Any
        ) -> None:
            if namespace == TASK_NAMESPACE and value["status"] == failed_status:
                raise RuntimeError("storage unavailable")
            await self.delegate.aput(namespace, key, value, **kwargs)

        async def aget(self, *args: Any, **kwargs: Any) -> Any:
            return await self.delegate.aget(*args, **kwargs)

    calls = 0

    async def runner(_record: TaskRecord, _control: object) -> str:
        nonlocal calls
        calls += 1
        return "finished"

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        tasks = TaskManager(FailingStore(runtime.store), WorktreeManager(tmp_path / "worktrees"))
        record = await tasks.spawn(
            parent_thread_id="session-parent",
            role="planner",
            prompt="inspect",
            workspace=tmp_path,
            worktree_enabled=False,
            runner=runner,
        )
        with pytest.raises(RuntimeError, match="storage unavailable"):
            await tasks.wait(record.task_id)
        assert not tasks.is_active(record.task_id)
        assert calls == (0 if failed_status == "running" else 1)
        # 启动前的暂时性写入故障可记为失败；终态写入失败只能标为待确认。
        recovered = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))
        if failed_status == "running":
            assert (await recovered.get(record.task_id)).status == "failed"
            assert (await recovered.get(record.task_id)).turn_seq == 1
            assert await recovered.reconcile_orphans() == []
        else:
            assert (await recovered.reconcile_orphans())[0].status == "interrupted"


@pytest.mark.asyncio
async def test_status_observer_failure_does_not_stop_task_runner(
    tmp_path: Path,
) -> None:
    called = 0
    notices: list[str] = []

    async def runner(_record: TaskRecord, _control: object) -> str:
        nonlocal called
        called += 1
        return "finished"

    async def callback(record: TaskRecord) -> None:
        if record.status == "running":
            raise RuntimeError("status observer failed")
        if record.status == "idle":
            notices.append(record.result or "")

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        tasks = TaskManager(
            runtime.store, WorktreeManager(tmp_path / "worktrees"), on_update=callback
        )
        record = await tasks.spawn(
            parent_thread_id="session-parent",
            role="planner",
            prompt="inspect",
            workspace=tmp_path,
            worktree_enabled=False,
            runner=runner,
        )
        await tasks.wait(record.task_id)
        saved = await tasks.get(record.task_id)
        assert saved.status == "idle"
        assert saved.turn_seq == 1
        assert saved.result == "finished"
        assert notices == ["finished"]
        assert called == 1
        assert not tasks.is_active(record.task_id)


@pytest.mark.asyncio
async def test_skipped_settled_callback_is_not_reported_as_repaired(tmp_path: Path) -> None:
    async def skipped_callback(_record: TaskRecord) -> str:
        return "skipped"

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        record = TaskRecord(
            task_id="skipped-notice",
            thread_id="task-skipped-notice",
            parent_thread_id="session-parent",
            role="planner",
            prompt="inspect",
            workspace=str(tmp_path.resolve()),
            worktree_enabled=False,
            status="idle",
            last_outcome="completed",
            turn_seq=1,
        )
        await runtime.store.aput(
            TASK_NAMESPACE, record.task_id, record.to_store_dict(), index=False
        )
        tasks = TaskManager(
            runtime.store, WorktreeManager(tmp_path / "worktrees"), on_settled=skipped_callback
        )
        assert await tasks.reconcile_settled_notifications(workspace=tmp_path) == []
        with pytest.raises(TaskError, match="notice was not stored"):
            await tasks.resume(record.task_id, lambda *_args: asyncio.sleep(0))
        assert not tasks.is_active(record.task_id)
        assert (await tasks.get(record.task_id)).turn_seq == 1


@pytest.mark.asyncio
async def test_resuming_idle_task_keeps_queued_followup_until_new_turn_finishes(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()

    async def runner(_record: TaskRecord, _control: object) -> str:
        await release.wait()
        return "第二轮完成"

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        inbox = TaskInbox(runtime.store)
        record = TaskRecord(
            task_id="resume-queue",
            thread_id="task-resume-queue",
            parent_thread_id="session-parent",
            role="planner",
            prompt="检查",
            workspace=str(tmp_path.resolve()),
            worktree_enabled=False,
            status="idle",
            result="第一轮完成",
            last_outcome="completed",
            turn_seq=1,
        )
        await runtime.store.aput(
            TASK_NAMESPACE, record.task_id, record.to_store_dict(), index=False
        )
        await inbox.ensure_settled(record, notice_limit_bytes=1024)
        await inbox.send(
            sender_thread_id="session-parent",
            receiver_thread_id=record.thread_id,
            task_id=record.task_id,
            kind="user_followup",
            content="稍后继续",
            queued=True,
            message_id="followup-after-resume",
        )

        async def after_idle(current: TaskRecord) -> None:
            await inbox.promote_next(current.thread_id)

        tasks = TaskManager(
            runtime.store,
            WorktreeManager(tmp_path / "worktrees"),
            on_settled=lambda current: inbox.ensure_settled(
                current, notice_limit_bytes=1024
            ),
            on_idle=after_idle,
        )
        await tasks.update(record)
        assert (await inbox.get("followup-after-resume")).status == "queued"
        resumed = await tasks.resume(record.task_id, runner)
        assert resumed.status == "pending"
        assert (await inbox.get("followup-after-resume")).status == "queued"
        release.set()
        await tasks.wait(record.task_id)
        assert (await inbox.get("followup-after-resume")).status == "pending"


@pytest.mark.asyncio
async def test_missing_settled_notice_is_repaired_after_restart(tmp_path: Path) -> None:
    state = tmp_path / "state"
    runs = 0
    record_id = ""

    async def runner(_record: TaskRecord, _control: object) -> str:
        nonlocal runs
        runs += 1
        return "检查完毕"

    async def broken_callback(record: TaskRecord) -> str:
        if record.status == "idle":
            raise RuntimeError("notification unavailable")
        return "skipped"

    async with await AgentRuntime.open(state) as runtime:
        tasks = TaskManager(
            runtime.store,
            WorktreeManager(tmp_path / "worktrees"),
            on_settled=broken_callback,
        )
        record = await tasks.spawn(
            parent_thread_id="session-parent",
            role="planner",
            prompt="inspect",
            workspace=tmp_path,
            worktree_enabled=False,
            runner=runner,
        )
        record_id = record.task_id
        with pytest.raises(RuntimeError, match="notification unavailable"):
            await tasks.wait(record_id)
        assert not tasks.is_active(record_id)
        assert (await tasks.get(record_id)).status == "idle"
        assert (await tasks.get(record_id)).turn_seq == 1
        assert await runtime.store.aget(INBOX_NAMESPACE, f"settled:{record_id}:1") is None
        with pytest.raises(RuntimeError, match="notification unavailable"):
            await tasks.resume(record_id, runner, prompt="下一轮")
        assert runs == 1
        assert not tasks.is_active(record_id)

    async with await AgentRuntime.open(state) as runtime:
        inbox = TaskInbox(runtime.store)
        callbacks = 0

        async def repaired_callback(record: TaskRecord) -> str:
            nonlocal callbacks
            callbacks += 1
            return await inbox.ensure_settled(record, notice_limit_bytes=1024)

        tasks = TaskManager(
            runtime.store,
            WorktreeManager(tmp_path / "worktrees"),
            on_settled=repaired_callback,
        )
        repaired = await tasks.reconcile_settled_notifications(workspace=tmp_path)
        assert [item.task_id for item in repaired] == [record_id]
        assert "检查完毕" in (await inbox.get(f"settled:{record_id}:1")).content
        assert await tasks.reconcile_settled_notifications(workspace=tmp_path) == []
        await inbox.acknowledge([f"settled:{record_id}:1"])
        assert await tasks.reconcile_settled_notifications(workspace=tmp_path) == []
        assert callbacks == 3
        assert runs == 1


@pytest.mark.asyncio
async def test_settled_notice_precedes_audit_failure(tmp_path: Path) -> None:
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        inbox = TaskInbox(runtime.store)

        class FailingAudit:
            async def append(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("audit unavailable")

        app = SimpleNamespace(
            task_inbox=inbox,
            audit=FailingAudit(),
            _notifications=asyncio.Queue(),
        )
        async def runner(_record: TaskRecord, _control: object) -> str:
            return "完成"

        tasks = TaskManager(
            runtime.store,
            WorktreeManager(tmp_path / "worktrees"),
            on_settled=lambda record: inbox.ensure_settled(record, notice_limit_bytes=1024),
            on_update=lambda record: on_task_update(app, record),
        )
        record = await tasks.spawn(
            parent_thread_id="session-parent",
            role="planner",
            prompt="inspect",
            workspace=tmp_path,
            worktree_enabled=False,
            runner=runner,
        )
        saved = await tasks.wait(record.task_id)
        assert saved.status == "idle"
        assert saved.result == "完成"
        assert "完成" in (await inbox.get(f"settled:{record.task_id}:1")).content


@pytest.mark.asyncio
async def test_non_git_builder_uses_shared_workspace_and_keeps_profile_private(
    tmp_path: Path,
) -> None:
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
            parent_thread_id="session",
            role="builder",
            prompt="edit",
            workspace=workspace,
            worktree_enabled=True,
            runner=runner,
            profile_name="temporary",
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
        saved_profile = (await tasks.get(record.task_id)).profile_snapshot
        assert saved_profile is not None
        assert saved_profile["api_key"] == "private-key"


@pytest.mark.asyncio
async def test_concurrent_resume_starts_only_one_runner(tmp_path: Path) -> None:
    release = asyncio.Event()
    runner_started = asyncio.Event()
    started = 0

    async def runner(_record: TaskRecord, _control: object) -> str:
        nonlocal started
        started += 1
        runner_started.set()
        await release.wait()
        return "done"

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        tasks = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))
        record = TaskRecord(
            task_id="concurrent",
            thread_id="task-concurrent",
            parent_thread_id=None,
            role="planner",
            prompt="inspect",
            workspace=str(tmp_path),
            worktree_enabled=False,
            status="idle",
        )
        await runtime.store.aput(
            TASK_NAMESPACE, record.task_id, record.to_store_dict(), index=False
        )
        original_get = tasks.get

        async def delayed_get(task_id: str) -> TaskRecord:
            # 强制两个调用在读取存储时交错，暴露检查活跃表与登记之间的竞态。
            await asyncio.sleep(0)
            return await original_get(task_id)

        tasks.get = delayed_get  # type: ignore[method-assign]
        attempts = await asyncio.gather(
            tasks.resume(record.task_id, runner),
            tasks.resume(record.task_id, runner),
            return_exceptions=True,
        )
        assert sum(isinstance(item, TaskRecord) for item in attempts) == 1
        assert sum(isinstance(item, TaskError) for item in attempts) == 1
        await asyncio.wait_for(runner_started.wait(), timeout=5)
        assert started == 1
        release.set()
        assert (await tasks.wait(record.task_id)).status == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_before_stop", [False, True])
async def test_stopped_task_replays_only_uncheckpointed_input(
    tmp_path: Path, checkpoint_before_stop: bool
) -> None:
    reached_graph = asyncio.Event()
    submitted: list[str | None] = []

    class CompletedStream:
        async def __aenter__(self) -> CompletedStream:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def __aiter__(self) -> CompletedStream:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

        async def interrupted(self) -> bool:
            return False

        async def output(self) -> dict[str, object]:
            return {"messages": [AIMessage(content="done")]}

        async def interrupts(self) -> list[object]:
            return []

    class DrainingRuntime:
        def __init__(self) -> None:
            self.drain_first = True
            self.messages: list[HumanMessage] = []

        async def get_state(self, _handle: object, _thread_id: str) -> SimpleNamespace:
            return SimpleNamespace(values={"messages": list(self.messages)}, next=())

        async def open_event_stream_v3(
            self,
            _handle: object,
            _context: object,
            message: str | None,
            *,
            control: Any,
            **_kwargs: object,
        ) -> CompletedStream:
            submitted.append(message)
            if self.drain_first:
                if checkpoint_before_stop:
                    assert message is not None
                    self.messages.append(HumanMessage(content=message, id="user-checkpoint"))
                reached_graph.set()
                while control.drain_reason is None:
                    await asyncio.sleep(0)
                raise GraphDrained
            return CompletedStream()

    async def get_handle(**_kwargs: object) -> tuple[object, object]:
        return object(), object()

    fake_runtime = DrainingRuntime()
    fake_app = SimpleNamespace(
        config=SimpleNamespace(profiles={}),
        profile_override=None,
        runtime=fake_runtime,
        _get_handle=get_handle,
        _thread_lock=lambda _thread_id: asyncio.Lock(),
        _audit_callback=lambda *_args, **_kwargs: None,
    )

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        tasks = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))

        async def runner(record: TaskRecord, control: Any) -> str | None:
            return await run_task(cast("SayacodeApp", fake_app), record, control)

        record = await tasks.spawn(
            parent_thread_id=None,
            role="planner",
            prompt="first task",
            workspace=tmp_path,
            worktree_enabled=False,
            runner=runner,
        )
        await reached_graph.wait()
        await tasks.stop(record.task_id)
        stopped = await tasks.wait(record.task_id)
        assert stopped.status == "stopped"
        assert stopped.pending_input == (None if checkpoint_before_stop else "first task")

        fake_runtime.drain_first = False
        await tasks.resume(record.task_id, runner)
        resumed = await tasks.wait(record.task_id)
        assert resumed.status == "idle"
        assert resumed.pending_input is None
        assert len(submitted) == 2
        assert submitted[0] is not None and "first task" in submitted[0]
        if checkpoint_before_stop:
            assert submitted[1] is None
        else:
            assert submitted[1] is not None and "first task" in submitted[1]


@pytest.mark.asyncio
async def test_old_reviewer_task_is_persisted_as_manual_approval(tmp_path: Path) -> None:
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        manager = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))
        record = TaskRecord(
            task_id="legacy",
            thread_id="task-legacy",
            parent_thread_id="session-parent",
            role="builder",
            prompt="检查文件",
            workspace=str(tmp_path),
            worktree_enabled=False,
        )
        stored = record.to_store_dict()
        stored["trust_level"] = "jev"
        await runtime.store.aput(TASK_NAMESPACE, record.task_id, stored, index=False)

        assert (await manager.get(record.task_id)).trust_level == "ask"
        assert (await runtime.store.aget(TASK_NAMESPACE, record.task_id)).value[
            "trust_level"
        ] == "ask"
