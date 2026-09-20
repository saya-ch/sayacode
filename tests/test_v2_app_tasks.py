from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.app import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths
from sayacode.runtime import AgentRuntime
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
        mode="build",
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
        write_access=True,
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
