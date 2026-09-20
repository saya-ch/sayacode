from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths
from sayacode.tasks import TaskRecord, WorktreeManager


class ScriptedModel(BaseChatModel):
    script: list[AIMessage] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "sayacode-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self


async def make_app(tmp_path: Path, model: BaseChatModel) -> SayacodeApp:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = AppPaths.resolve(tmp_path / "state")
    runtime = await AgentRuntime.open(paths.home)
    config = Config(
        default_profile="test",
        profiles={
            "test": Profile(
                name="test",
                protocol="openai_chat_completions",
                base_url="https://unused.test/v1",
                api_key="test-key",
                model_id="test",
                context_length=8192,
                max_output_tokens=512,
                file_search=False,
                summary_trigger_tokens=None,
                tool_selector_max_tools=None,
            )
        },
    )
    app = SayacodeApp(
        paths=paths,
        repository=ConfigRepository(paths.home),
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="session-test",
        trust_level="ask",
        profile_name="test",
        model_override=model,
    )
    return await app.initialize()


async def test_stream_approval_is_checkpointed_and_resumes_once(tmp_path: Path):
    model = ScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command_tool",
                        "args": {"command": "Write-Output okay"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="finished"),
        ]
    )
    app = await make_app(tmp_path, model)
    try:
        events = [event async for event in app.stream("run a command")]
        assert [event["type"] for event in events][-2:] == ["approval.requested", "run.paused"]
        result = await app.command(
            "approve",
            {"thread_id": "session-test", "decisions": [{"type": "approve"}]},
        )
        assert result == {
            "ok": True,
            "status": "completed",
            "thread_id": "session-test",
            "response": "finished",
        }
        history = await app.command("history", "")
        assert sum(row["role"] == "tool" for row in history) == 1
    finally:
        await app.aclose()


async def test_background_read_task_reports_completion(tmp_path: Path):
    app = await make_app(tmp_path, ScriptedModel(script=[AIMessage(content="review complete")]))
    try:
        record = await app._spawn_task(
            "review code", role="reviewer", parent_thread_id=app.session_id
        )
        completed = await app.command("team", f"wait {record.task_id}")
        assert completed["status"] == "completed"
        assert completed["result"] == "review complete"
        notification = await app.next_notification()
        assert notification["task_id"] == record.task_id
    finally:
        await app.aclose()


async def test_completed_child_starts_parent_graph_without_fake_user_message(
    tmp_path: Path,
) -> None:
    class RecordingModel(ScriptedModel):
        inputs: list[list] = []

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.inputs.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = RecordingModel(
        script=[
            AIMessage(content="child result"),
            AIMessage(content="parent continued"),
        ]
    )
    app = await make_app(tmp_path, model)
    try:
        record = await app._spawn_task(
            "review code", role="reviewer", parent_thread_id=app.session_id
        )
        outcomes = await app.wait_for_tasks()
        assert outcomes[0]["task_id"] == record.task_id
        assert outcomes[0]["parent_wake"]["type"] == "agent.wake.completed"
        assert outcomes[0]["parent_wake"]["response"] == "parent continued"
        assert model.calls == 2
        assert any(
            isinstance(message, SystemMessage)
            and "SAYACODE internal background-task event" in str(message.content)
            for message in model.inputs[1]
        )
        history = await app.command("history")
        assert not any(item["role"] == "human" for item in history)
        assert any(item["content"] == "parent continued" for item in history)
    finally:
        await app.aclose()


async def test_child_notification_waits_for_busy_parent_and_is_not_duplicated(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        script=[
            AIMessage(content="child result"),
            AIMessage(content="parent continued"),
        ]
    )
    app = await make_app(tmp_path, model)
    parent_lock = app._thread_lock(app.session_id)
    await parent_lock.acquire()
    try:
        record = await app._spawn_task(
            "review code", role="reviewer", parent_thread_id=app.session_id
        )
        await app.tasks.wait(record.task_id)
        await asyncio.sleep(0)
        assert model.calls == 1
    finally:
        parent_lock.release()
    try:
        await app.wait_for_tasks()
        assert model.calls == 2
        await app.tasks.update(await app.tasks.get(record.task_id))
        await asyncio.sleep(0)
        assert model.calls == 2
    finally:
        await app.aclose()


async def test_parent_tool_result_acknowledges_completion_before_auto_run(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        script=[
            AIMessage(content="child result"),
            AIMessage(content="should not run"),
        ]
    )
    app = await make_app(tmp_path, model)
    parent_lock = app._thread_lock(app.session_id)
    await parent_lock.acquire()
    try:
        record = await app._spawn_task(
            "review code", role="reviewer", parent_thread_id=app.session_id
        )
        completed = await app.tasks.wait(record.task_id)
        await app._acknowledge_task_event(completed, app.session_id)
    finally:
        parent_lock.release()
    try:
        await app.wait_for_tasks()
        assert model.calls == 1
        event = await app.runtime.store.aget(("sayacode", "parent_events"), f"{record.task_id}:1")
        assert event.value["state"] == "delivered"
    finally:
        await app.aclose()


async def test_pending_parent_notification_runs_after_process_restart(tmp_path: Path) -> None:
    first = await make_app(tmp_path, ScriptedModel(script=[AIMessage(content="unused")]))
    event_id = "finished-task:1"
    await first.runtime.store.aput(
        ("sayacode", "parent_events"),
        event_id,
        {
            "event_id": event_id,
            "parent_thread_id": first.session_id,
            "task_id": "finished-task",
            "role": "reviewer",
            "status": "completed",
            "state": "pending",
            "created_at": "2026-01-01T00:00:00Z",
        },
        index=False,
    )
    paths, config, workspace = first.paths, first.config, first.workspace
    await first.aclose()
    model = ScriptedModel(script=[AIMessage(content="recovered parent response")])
    runtime = await AgentRuntime.open(paths.home)
    second = SayacodeApp(
        paths=paths,
        repository=ConfigRepository(paths.home),
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="session-test",
        trust_level="ask",
        profile_name="test",
        model_override=model,
    )
    try:
        await second.initialize()
        await second.wait_for_tasks()
        event = await second.runtime.store.aget(("sayacode", "parent_events"), event_id)
        assert event.value["state"] == "delivered"
        assert model.calls == 1
        assert any(
            item["content"] == "recovered parent response"
            for item in await second.command("history")
        )
    finally:
        await second.aclose()


async def test_unconfirmed_parent_turn_is_not_replayed_on_restart(tmp_path: Path) -> None:
    first = await make_app(tmp_path, ScriptedModel(script=[AIMessage(content="unused")]))
    event_id = "uncertain-task:1"
    await first.runtime.store.aput(
        ("sayacode", "parent_events"),
        event_id,
        {
            "event_id": event_id,
            "parent_thread_id": first.session_id,
            "task_id": "uncertain-task",
            "role": "builder",
            "status": "completed",
            "state": "processing",
            "created_at": "2026-01-01T00:00:00Z",
        },
        index=False,
    )
    paths, config, workspace = first.paths, first.config, first.workspace
    await first.aclose()
    model = ScriptedModel(script=[AIMessage(content="must not repeat")])
    runtime = await AgentRuntime.open(paths.home)
    second = SayacodeApp(
        paths=paths,
        repository=ConfigRepository(paths.home),
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="session-test",
        trust_level="ask",
        profile_name="test",
        model_override=model,
    )
    try:
        await second.initialize()
        await second.wait_for_tasks()
        event = await second.runtime.store.aget(("sayacode", "parent_events"), event_id)
        assert event.value["state"] == "uncertain"
        assert model.calls == 0
        notice = await second.next_notification()
        assert notice["type"] == "agent.wake.uncertain"
    finally:
        await second.aclose()


async def test_parent_wake_uses_native_approval_and_resumes_once(tmp_path: Path) -> None:
    model = ScriptedModel(
        script=[
            AIMessage(content="child result"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command_tool",
                        "args": {"command": "Write-Output should-not-run"},
                        "id": "wake-shell-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="approval rejected; task result reviewed"),
        ]
    )
    app = await make_app(tmp_path, model)
    try:
        record = await app._spawn_task(
            "review code", role="reviewer", parent_thread_id=app.session_id
        )
        outcomes = await app.wait_for_tasks()
        assert outcomes[0]["parent_wake"]["type"] == "agent.wake.paused"
        pending = await app.pending_approval(app.session_id)
        assert pending["status"] == "paused"
        assert pending["action_requests"][0]["name"] == "execute_command_tool"
        resumed = await app.command(
            "reject",
            {
                "thread_id": app.session_id,
                "decisions": [{"type": "reject", "message": "Declined in test"}],
            },
        )
        assert resumed["status"] == "completed"
        assert model.calls == 3
        event = await app.runtime.store.aget(("sayacode", "parent_events"), f"{record.task_id}:1")
        assert event.value["state"] == "delivered"
    finally:
        await app.aclose()


async def test_full_trust_builder_can_write_outside_its_worktree(tmp_path: Path) -> None:
    outside = tmp_path / "outside-worktree.txt"
    model = ScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": str(outside), "content": "global edit"},
                        "id": "global-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="builder finished"),
            AIMessage(content="parent saw the result"),
        ]
    )
    app = await make_app(tmp_path, model)
    git(app.workspace, "init")
    git(app.workspace, "config", "user.name", "Test")
    git(app.workspace, "config", "user.email", "test@example.invalid")
    (app.workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(app.workspace, "add", "tracked.txt")
    git(app.workspace, "commit", "-m", "base")
    try:
        await app.command("trust", "full")
        record = await app._spawn_task(
            "write outside", role="builder", parent_thread_id=app.session_id
        )
        outcomes = await app.wait_for_tasks()
        assert outcomes[0]["status"] == "completed"
        assert record.worktree_enabled and Path(record.task_workspace or "").is_dir()
        assert outside.read_text(encoding="utf-8") == "global edit"
        assert (await app.tasks.delivery(record.task_id))["patch"] == ""
    finally:
        await app.aclose()


async def test_langchain_callbacks_supply_local_trace_metadata(tmp_path: Path):
    app = await make_app(tmp_path, ScriptedModel(script=[AIMessage(content="traced")]))
    try:
        assert (await app.run("record this"))["ok"] is True
        trace = await app.command("trace", "")
        events = {item["event"] for item in trace}
        assert "model.started" in events
        assert "model.completed" in events
        assert all(item["thread_id"] == app.session_id for item in trace)
    finally:
        await app.aclose()


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_worktree_snapshot_preserves_dirty_source_and_delivery_is_explicit(tmp_path: Path):
    source = tmp_path / "repository"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "base")
    (source / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (source / "untracked.txt").write_text("snapshot\n", encoding="utf-8")

    manager = WorktreeManager(tmp_path / "worktrees")
    snapshot = manager.create("task123", source)
    assert (snapshot.root / "tracked.txt").read_text(encoding="utf-8") == "dirty\n"
    assert (snapshot.root / "untracked.txt").read_text(encoding="utf-8") == "snapshot\n"
    (snapshot.root / "delivery.txt").write_text("new\n", encoding="utf-8")
    record = TaskRecord(
        task_id="task123",
        thread_id="task-thread",
        parent_thread_id=None,
        role="builder",
        prompt="implement",
        workspace=str(source),
        worktree_enabled=True,
        worktree_root=str(snapshot.root),
        task_workspace=str(snapshot.workspace),
        branch=snapshot.branch,
        snapshot_commit=snapshot.snapshot_commit,
    )
    delivery = manager.inspect(record)
    assert "delivery.txt" in delivery["patch"]
    assert not (source / "delivery.txt").exists()
    applied = manager.apply_delivery(record)
    assert applied["applied"] is True
    assert (source / "delivery.txt").read_text(encoding="utf-8") == "new\n"
