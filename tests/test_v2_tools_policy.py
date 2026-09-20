"""Real tool execution and official HITL contract checks without a provider."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sayacode.policy import Policy, PolicyMiddleware, build_approval_middleware
from sayacode.tools import (
    FileEdit,
    batch_edit,
    build_tools,
    execute_command_tool,
    read_output_file,
    search_replace,
    write_file,
)


def context(root: Path, mode: str = "build", policy: Policy | None = None):
    return SimpleNamespace(
        workspace=root, output_dir=root / ".outputs", mode=mode, policy=policy or Policy()
    )


def tool_runtime(root: Path) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=context(root),
        config={},
        stream_writer=lambda _: None,
        tool_call_id="test",
        store=None,
    )


class ToolCallingModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_runtime_is_not_exposed_to_the_model():
    for item in build_tools():
        properties = item.tool_call_schema.model_json_schema().get("properties", {})
        assert "runtime" not in properties


def test_policy_precedence_and_boundaries(tmp_path):
    policy = Policy(
        user={"*": "allow"}, project={"write_file": "deny"}, session={"write_file": "allow"}
    )
    assert (
        policy.decide("write_file", {"path": "a.txt"}, context(tmp_path, policy=policy)).action
        == "deny"
    )
    policy.project["write_file"] = "ask"
    assert (
        policy.decide("write_file", {"path": "a.txt"}, context(tmp_path, policy=policy)).action
        == "allow"
    )
    assert (
        policy.decide("write_file", {"path": "a.txt"}, context(tmp_path, "review", policy)).action
        == "deny"
    )
    assert (
        policy.decide(
            "write_file", {"path": "../outside.txt"}, context(tmp_path, policy=policy)
        ).action
        == "deny"
    )
    assert (
        policy.decide("read_file", {"path": ".env"}, context(tmp_path, policy=policy)).action
        == "deny"
    )
    assert (
        policy.decide(
            "read_file", {"path": ".env.example"}, context(tmp_path, policy=policy)
        ).action
        == "allow"
    )
    assert Policy().decide("git", {"action": "status"}, context(tmp_path, "plan")).action == "allow"
    assert Policy().decide("git", {"action": "push"}, context(tmp_path, "plan")).action == "deny"
    assert Policy().decide("grep_search", {"path": "/"}, context(tmp_path)).action == "allow"
    assert (
        Policy().decide("execute_command_tool", {"command": "echo hi"}, context(tmp_path)).action
        == "ask"
    )
    assert Policy().decide("mcp_remote_tool", {}, context(tmp_path)).action == "ask"
    assert (
        Policy().decide("web_search", {"query": "docs"}, context(tmp_path, "plan")).action == "ask"
    )


def test_exact_edit_preserves_newlines_and_validates_whole_batch(tmp_path):
    runtime = tool_runtime(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"alpha\r\nbeta\r\n")
    search_replace.func(path="a.txt", old_text="alpha", new_text="first", runtime=runtime)
    assert (tmp_path / "a.txt").read_bytes() == b"first\r\nbeta\r\n"
    with pytest.raises(ValueError, match="not found"):
        batch_edit.func(
            edits=[
                FileEdit(path="a.txt", old_text="first", new_text="changed"),
                FileEdit(path="a.txt", old_text="absent", new_text="oops"),
            ],
            runtime=runtime,
        )
    assert (tmp_path / "a.txt").read_bytes() == b"first\r\nbeta\r\n"
    with pytest.raises(PermissionError):
        write_file.func(path="../escape.txt", content="bad", runtime=runtime)


@pytest.mark.asyncio
async def test_read_only_graph_denies_writes_without_an_interrupt(tmp_path):
    model = ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "x.txt", "content": "bad"},
                        "id": "write-1",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(
        model,
        [write_file],
        middleware=[PolicyMiddleware(), build_approval_middleware([write_file])],
        checkpointer=InMemorySaver(),
    )
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "review"}]},
        {"configurable": {"thread_id": "readonly"}},
        context=context(tmp_path, "review"),
    )
    assert not (tmp_path / "x.txt").exists()
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(messages) == 1 and messages[0].status == "error"


@pytest.mark.asyncio
async def test_official_hitl_approval_runs_shell_once(tmp_path):
    command = (
        "Set-Content -LiteralPath result.txt -Value approved"
        if os.name == "nt"
        else "printf approved > result.txt"
    )
    model = ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "execute_command_tool", "args": {"command": command}, "id": "shell-1"}
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(
        model,
        [execute_command_tool],
        middleware=[PolicyMiddleware(), build_approval_middleware([execute_command_tool])],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "approval"}}
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "run"}]}, config, context=context(tmp_path)
    )
    assert result["__interrupt__"]
    assert not (tmp_path / "result.txt").exists()
    result = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}), config, context=context(tmp_path)
    )
    assert (tmp_path / "result.txt").read_text().strip() == "approved"
    assert not result.get("__interrupt__")


@pytest.mark.asyncio
async def test_process_timeout_and_output_locator(tmp_path):
    runtime = tool_runtime(tmp_path)
    command = "Write-Output hello" if os.name == "nt" else "printf hello"
    result = await execute_command_tool.coroutine(command=command, runtime=runtime)
    assert result["exit_code"] == 0 and "hello" in result["stdout"]
    assert "hello" in read_output_file.func(path=result["stdout_file"], runtime=runtime)
    sleep = "Start-Sleep -Seconds 20" if os.name == "nt" else "sleep 20"
    result = await execute_command_tool.coroutine(command=sleep, runtime=runtime, timeout=0.2)
    assert result["timed_out"] and result["exit_code"] != 0
    with pytest.raises(PermissionError):
        read_output_file.func(path="../outside.txt", runtime=runtime)
