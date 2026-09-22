"""Real LangGraph v3 output and persisted mixed-approval regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.cli.approvals import _pending_team_approval, _resume_approval_from_terminal
from sayacode.cli.main import amain
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths


class StreamingModel(FakeListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class ScriptedModel(BaseChatModel):
    script: list[AIMessage] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "v2-regression-scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self


async def _app(
    tmp_path: Path, model: BaseChatModel, *, session_id: str = "session-one"
) -> SayacodeApp:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    paths = AppPaths.resolve(tmp_path / "state")
    repository = ConfigRepository(paths.home)
    if repository.path.exists():
        config = await repository.load()
    else:
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
                    summary_trigger_ratio=None,
                    model_retries=0,
                    tool_retries=0,
                    tool_selector_max_tools=None,
                )
            },
        )
        await repository.save(config)
    runtime = await AgentRuntime.open(paths.home)
    app = SayacodeApp(
        paths=paths,
        repository=repository,
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id=session_id,
        trust_level="ask",
        profile_name="test",
        model_override=model,
    )
    return await app.initialize()


@pytest.mark.asyncio
async def test_native_v3_model_stream_exposes_only_visible_text(tmp_path: Path) -> None:
    app = await _app(tmp_path, StreamingModel(responses=["hello"]))
    try:
        events = [event async for event in app.stream("say hello")]
        deltas = [event["delta"] for event in events if event["type"] == "assistant.delta"]
        assert "".join(deltas) == "hello"
        assert all(
            "message-start" not in delta and "content-block" not in delta for delta in deltas
        )
        assert events[-1]["type"] == "run.completed"
        assert events[-1]["response"] == "hello"
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_real_graph_jsonl_contains_visible_text_and_one_terminal_event(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    app = await _app(tmp_path, StreamingModel(responses=["hello"]))
    monkeypatch.setenv("SAYACODE_HOME", str(app.paths.home))
    code = await amain(
        ["--workspace", str(app.workspace), "-p", "say hello", "--output-format", "jsonl"],
        app_factory=lambda args: app,
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert "".join(item["delta"] for item in events if item["type"] == "assistant.delta") == "hello"
    assert [item["type"] for item in events].count("run.completed") == 1
    assert events[-1]["response"] == "hello"


@pytest.mark.asyncio
async def test_native_v3_tool_start_and_finish_keep_call_identity(tmp_path: Path) -> None:
    app = await _app(
        tmp_path,
        ScriptedModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "sample.txt"},
                            "id": "read-1",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        ),
    )
    (app.workspace / "sample.txt").write_text("sample content", encoding="utf-8")
    try:
        events = [event async for event in app.stream("read sample.txt")]
        started = [event for event in events if event["type"] == "tool.started"]
        completed = [event for event in events if event["type"] == "tool.completed"]
        assert len(started) == len(completed) == 1
        assert started[0]["tool_name"] == completed[0]["tool_name"] == "read_file"
        assert started[0]["tool_call_id"] == completed[0]["tool_call_id"] == "read-1"
        assert events[-1]["type"] == "run.completed"
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_native_v3_tool_error_is_not_reported_as_start(tmp_path: Path) -> None:
    @tool
    def explode() -> str:
        """Raise an intentional test error."""
        raise RuntimeError("boom")

    model = ScriptedModel(
        script=[
            AIMessage(content="", tool_calls=[{"name": "explode", "args": {}, "id": "boom-1"}]),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(model, tools=[explode], checkpointer=InMemorySaver())
    run = await graph.astream_events(
        {"messages": [{"role": "user", "content": "call explode"}]},
        {"configurable": {"thread_id": "tool-error"}},
        version="v3",
    )
    raw_tools: list[dict[str, Any]] = []
    try:
        async with run:
            async for event in run:
                data = event.get("params", {}).get("data")
                if event.get("method") == "tools" and isinstance(data, dict):
                    raw_tools.append(event)
    except RuntimeError as exc:
        assert "boom" in str(exc)
    assert [event["params"]["data"]["event"] for event in raw_tools] == [
        "tool-started",
        "tool-error",
    ]
    app = await _app(tmp_path, StreamingModel(responses=["unused"]))
    try:
        public = [item for event in raw_tools for item in app._normalize_event(event, "tool-error")]
        assert public[0]["type"] == "tool.started"
        assert public[0]["tool_name"] == "explode"
        assert public[0]["tool_call_id"] == "boom-1"
        failed = public[-1:]
        assert failed == [
            {
                "type": "tool.failed",
                "thread_id": "tool-error",
                "tool_call_id": "boom-1",
                "tool_name": "explode",
                "tool_input": {},
                "error": "boom",
            }
        ]
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_mixed_approval_affects_only_approved_call_and_survives_reopen(
    tmp_path: Path,
) -> None:
    first = {"path": "first.txt"}
    second = {"path": "second.txt"}
    model = ScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delete_file", "args": first, "id": "delete-1"},
                    {"name": "delete_file", "args": second, "id": "delete-2"},
                ],
            ),
            AIMessage(content="finished"),
        ]
    )
    app = await _app(tmp_path, model)
    first_path = app.workspace / first["path"]
    second_path = app.workspace / second["path"]
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    try:
        events = [event async for event in app.stream("delete only the first file")]
        pending = next(event for event in events if event["type"] == "approval.requested")
        assert len(pending["action_requests"]) == 2
        assert first_path.exists() and second_path.exists()
        result = await app.command(
            "approve",
            {
                "thread_id": app.session_id,
                "decisions": [
                    {"type": "approve"},
                    {"type": "reject", "message": "Keep second.txt"},
                ],
                "grants": [{"index": 0, "tool_name": "delete_file"}],
            },
        )
        assert result["ok"] is True and result["status"] == "completed"
        assert not first_path.exists() and second_path.exists()
        context = app._context(app.session_id, "ask")
        assert context.policy.decide("delete_file", first, context).action == "allow"
        assert context.policy.decide("delete_file", second, context).action == "ask"
    finally:
        await app.aclose()

    reopened = await _app(tmp_path, StreamingModel(responses=["unused"]))
    try:
        context = reopened._context("session-one", "ask")
        assert context.policy.decide("delete_file", first, context).action == "allow"
        assert context.policy.decide("delete_file", second, context).action == "ask"
        other = await reopened.command("session", "new other")
        context_other = reopened._context(other["session_id"], "ask")
        assert context_other.policy.decide("delete_file", first, context_other).action == "ask"
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_paused_background_task_has_public_pending_and_reject_path(tmp_path: Path) -> None:
    model = ScriptedModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_command_tool",
                        "args": {"command": "echo example"},
                        "id": "shell-1",
                    },
                ],
            ),
            AIMessage(content="review complete without search"),
        ]
    )
    app = await _app(tmp_path, model)
    try:
        record = await app._spawn_task(
            "review without external search", role="reviewer", parent_thread_id=app.session_id
        )
        settled = await app.wait_for_tasks()
        assert len(settled) == 1 and settled[0]["status"] == "paused"
        pending = await _pending_team_approval(app, record.task_id)
        assert pending["thread_id"] == record.thread_id
        assert [action["name"] for action in pending["action_requests"]] == ["execute_command_tool"]
        resolved = await _resume_approval_from_terminal(app, pending, object(), reject_all=True)
        assert resolved["ok"] is True
        assert resolved["response"] == "review complete without search"
        assert (await app.tasks.get(record.task_id)).status == "idle"
    finally:
        await app.aclose()

