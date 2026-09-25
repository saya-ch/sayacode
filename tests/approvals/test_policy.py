"""不依赖模型服务商的真实工具策略与官方 HITL 契约。"""

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

from sayacode.approvals import Policy, PolicyMiddleware, build_approval_middleware
from sayacode.tools import (
    build_tools,
    delete_file,
    execute_command_tool,
    read_output_file,
    search_replace,
    write_file,
)


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
    assert policy.decide("execute_command_tool", {"command": "echo hi"}, ctx).action == "deny"


def test_workspace_auto_limits_file_tools_but_asks_for_every_shell_call(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    policy = Policy(trust_level="workspace_auto")
    ctx = context(workspace, "workspace_auto", policy)
    assert policy.decide("read_file", {"path": str(outside)}, ctx).action == "allow"
    for name in ("write_file", "search_replace", "delete_file"):
        assert policy.decide(name, {"path": "nested/file.txt"}, ctx).action == "allow"
        assert policy.decide(name, {"path": str(outside)}, ctx).action == "deny"
    command = {"command": "echo hi"}
    policy.grant_call("execute_command_tool", command, ctx)
    assert policy.decide("execute_command_tool", command, ctx).action == "ask"
    assert policy.decide("mcp__external", {}, ctx).action == "ask"

    runtime = ToolRuntime(
        state={},
        context=ctx,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="workspace-auto",
        store=None,
    )
    write_file.func(path="new.txt", content="inside", runtime=runtime)
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "inside"
    with pytest.raises(PermissionError):
        write_file.func(path=str(outside), content="outside", runtime=runtime)
    assert not outside.exists()


def test_planner_and_reviewer_cannot_expand_inherited_full_trust(tmp_path):
    for role in ("planner", "reviewer"):
        policy = Policy(trust_level="full")
        ctx = context(tmp_path, "full", policy)
        ctx.agent_role = role
        assert policy.decide("read_file", {"path": "source.py"}, ctx).action == "allow"
        assert policy.decide("write_file", {"path": "source.py"}, ctx).action == "deny"
        assert policy.decide("execute_command_tool", {"command": "echo hi"}, ctx).action == "deny"


def test_workspace_auto_resolves_symlink_before_file_write(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"此环境无法建立目录符号链接：{error}")
    policy = Policy(trust_level="workspace_auto")
    ctx = context(workspace, "workspace_auto", policy)
    runtime = ToolRuntime(
        state={},
        context=ctx,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="symlink",
        store=None,
    )
    assert policy.decide("write_file", {"path": "linked/escape.txt"}, ctx).action == "deny"
    with pytest.raises(PermissionError):
        write_file.func(path="linked/escape.txt", content="bad", runtime=runtime)
    assert not (outside / "escape.txt").exists()


def test_workspace_auto_cannot_delete_link_located_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = workspace / "safe.txt"
    target.write_text("safe", encoding="utf-8")
    link = outside / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"此环境无法建立文件符号链接：{error}")
    ctx = context(workspace, "workspace_auto")
    assert ctx.policy.decide("delete_file", {"path": str(link)}, ctx).action == "deny"
    runtime = ToolRuntime(
        state={},
        context=ctx,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="outside-link",
        store=None,
    )
    with pytest.raises(PermissionError):
        delete_file.func(path=str(link), runtime=runtime)
    assert link.is_symlink() and target.read_text(encoding="utf-8") == "safe"


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
async def test_workspace_auto_graph_writes_inside_and_denies_outside_without_interrupt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    model = ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "write_file", "args": {"path": "inside.txt", "content": "ok"}, "id": "in"},
                    {
                        "name": "write_file",
                        "args": {"path": str(outside), "content": "bad"},
                        "id": "out",
                    },
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
        {"messages": [{"role": "user", "content": "edit"}]},
        {"configurable": {"thread_id": "workspace-auto"}},
        context=context(workspace, "workspace_auto"),
    )
    assert not result.get("__interrupt__")
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "ok"
    assert not outside.exists()
    messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert sorted(item.status for item in messages) == ["error", "success"]


@pytest.mark.parametrize("trust_level", ["ask", "workspace_auto"])
@pytest.mark.asyncio
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
