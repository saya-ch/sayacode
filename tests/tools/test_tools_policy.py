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

from sayacode.tools import (
    build_tools,
    delete_file,
    execute_command_tool,
    read_output_file,
    search_replace,
    write_file,
)
from sayacode.trust import Policy, PolicyMiddleware, build_approval_middleware


def context(root: Path, trust_level: str = "ask", policy: Policy | None = None):
    return SimpleNamespace(
        workspace=root,
        output_dir=root / ".outputs",
        trust_level=trust_level,
        policy=policy or Policy(trust_level=trust_level),
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


def test_global_paths_and_three_trust_levels(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    policy = Policy(trust_level="ask")
    ctx = context(tmp_path, policy=policy)
    assert policy.decide("read_file", {"path": str(outside)}, ctx).action == "allow"
    assert policy.decide("write_file", {"path": str(outside)}, ctx).action == "ask"
    assert policy.decide("mcp__external", {}, ctx).action == "ask"
    policy.trust_level = "full"
    assert policy.decide("write_file", {"path": str(outside)}, ctx).action == "allow"
    policy.trust_level = "read_only"
    assert policy.decide("write_file", {"path": str(outside)}, ctx).action == "deny"
    assert policy.decide("execute_command_tool", {"command": "echo hi"}, ctx).action == "ask"


def test_exact_edit_preserves_newlines_and_global_file_access(tmp_path):
    runtime = tool_runtime(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"alpha\r\nbeta\r\n")
    search_replace.func(path="a.txt", old_text="alpha", new_text="first", runtime=runtime)
    assert (tmp_path / "a.txt").read_bytes() == b"first\r\nbeta\r\n"
    with pytest.raises(ValueError, match="not found"):
        search_replace.func(path="a.txt", old_text="absent", new_text="oops", runtime=runtime)
    assert (tmp_path / "a.txt").read_bytes() == b"first\r\nbeta\r\n"
    outside = tmp_path.parent / f"{tmp_path.name}-global-write.txt"
    write_file.func(path=str(outside), content="allowed", runtime=runtime)
    assert outside.read_text(encoding="utf-8") == "allowed"


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
        context=context(tmp_path, "read_only"),
    )
    assert not (tmp_path / "x.txt").exists()
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(messages) == 1 and messages[0].status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_level", ["ask", "read_only"])
async def test_official_hitl_approval_runs_shell_once(tmp_path, trust_level):
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
        {"messages": [{"role": "user", "content": "run"}]},
        config,
        context=context(tmp_path, trust_level),
    )
    assert result["__interrupt__"]
    assert not (tmp_path / "result.txt").exists()
    result = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config,
        context=context(tmp_path, trust_level),
    )
    assert (tmp_path / "result.txt").read_text().strip() == "approved"
    assert not result.get("__interrupt__")


@pytest.mark.asyncio
async def test_native_parallel_different_tools_receive_separate_approvals(tmp_path):
    existing = tmp_path / "keep.txt"
    existing.write_text("keep", encoding="utf-8")
    model = ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "new.txt", "content": "created"},
                        "id": "write-1",
                    },
                    {"name": "delete_file", "args": {"path": "keep.txt"}, "id": "delete-1"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    tools = [write_file, delete_file]
    graph = create_agent(
        model,
        tools,
        middleware=[PolicyMiddleware(), build_approval_middleware(tools)],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "parallel-approval"}}
    ctx = context(tmp_path, "ask")
    pending = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "apply both"}]}, config, context=ctx
    )
    assert len(pending["__interrupt__"][0].value["action_requests"]) == 2
    assert not (tmp_path / "new.txt").exists() and existing.exists()
    completed = await graph.ainvoke(
        Command(
            resume={
                "decisions": [
                    {"type": "approve"},
                    {"type": "reject", "message": "Keep file"},
                ]
            }
        ),
        config,
        context=ctx,
    )
    assert not completed.get("__interrupt__")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "created"
    assert existing.read_text(encoding="utf-8") == "keep"


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


@pytest.mark.asyncio
async def test_shell_accepts_absolute_cwd_outside_starting_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-shell-cwd"
    outside.mkdir()
    command = "(Get-Location).Path" if os.name == "nt" else "pwd"
    result = await execute_command_tool.coroutine(
        command=command, cwd=str(outside), runtime=tool_runtime(tmp_path)
    )
    assert result["exit_code"] == 0
    assert str(outside).lower() in result["stdout"].strip().lower()
