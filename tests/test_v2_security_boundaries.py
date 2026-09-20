"""Regression coverage for the reviewed authorization and process boundaries."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP
from langchain.agents import create_agent
from langchain.agents.middleware import FilesystemFileSearchMiddleware
from langchain.mcp import MCPAdapter
from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sayacode.policy import Policy, PolicyMiddleware, build_approval_middleware
from sayacode.tools import (
    attach_process_tree,
    close_process_tree,
    git,
    namespace_mcp_tools,
    process_creation_options,
    stop_process_tree,
    write_file,
)


def context(root: Path, policy: Policy | None = None):
    return SimpleNamespace(
        workspace=root, output_dir=root / "out", trust_level="ask", policy=policy or Policy()
    )


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.parametrize("use_ripgrep", [True, False])
async def test_official_search_has_no_sensitive_path_exceptions(tmp_path, use_ripgrep):
    (tmp_path / "credentials.json").write_text("MATCH_SECRET_CREDENTIAL", encoding="utf-8")
    (tmp_path / ".env").write_text("MATCH_SECRET_ENV", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("MATCH_SECRET_KEY", encoding="utf-8")
    (tmp_path / "source.txt").write_text("MATCH_PUBLIC_CODE", encoding="utf-8")
    search = FilesystemFileSearchMiddleware(root_path=str(tmp_path), use_ripgrep=use_ripgrep)
    result = await search.grep_search.ainvoke(
        {"pattern": "MATCH_", "path": "/", "output_mode": "content"}
    )
    assert "MATCH_PUBLIC_CODE" in result
    assert "MATCH_SECRET" in result
    paths = await search.glob_search.ainvoke({"pattern": "**/*", "path": "/"})
    assert "source.txt" in paths
    assert "credentials.json" in paths


def test_call_grants_are_bound_to_workspace_command_and_arguments(tmp_path):
    policy = Policy(trust_level="ask")
    ctx = context(tmp_path, policy)
    key = policy.grant_call("execute_command_tool", {"command": "python -m pytest"}, ctx)
    assert key in policy.session_grants
    assert "python -m pytest" not in key
    assert (
        policy.decide(
            "execute_command_tool", {"command": "python -m pytest"}, ctx
        ).action
        == "allow"
    )
    assert (
        policy.decide("execute_command_tool", {"command": "python -m pip install x"}, ctx).action
        == "ask"
    )
    assert (
        policy.decide(
            "execute_command_tool", {"command": "python -m pytest", "cwd": "subdir"}, ctx
        ).action
        == "ask"
    )
    assert (
        policy.decide(
            "execute_command_tool",
            {"command": "python -m pytest"},
            context(tmp_path / "other", policy),
        ).action
        == "ask"
    )


@pytest.mark.parametrize(
    ("level", "write_action", "shell_action", "mcp_action"),
    [
        ("read_only", "deny", "ask", "deny"),
        ("ask", "ask", "ask", "ask"),
        ("full", "allow", "allow", "allow"),
    ],
)
def test_three_global_trust_levels(tmp_path, level, write_action, shell_action, mcp_action):
    policy = Policy(trust_level=level)
    ctx = context(tmp_path, policy)
    assert policy.decide("read_file", {"path": str(tmp_path / ".env")}, ctx).action == "allow"
    assert policy.decide("web_search", {"query": "docs"}, ctx).action == "allow"
    assert policy.decide("write_file", {"path": "../outside.txt"}, ctx).action == write_action
    assert policy.decide("execute_command_tool", {"command": "echo hi"}, ctx).action == shell_action
    assert policy.decide("mcp__unknown", {}, ctx).action == mcp_action


def test_read_only_shell_never_remembers_approval(tmp_path):
    policy = Policy(trust_level="read_only")
    ctx = context(tmp_path, policy)
    arguments = {"command": "echo hi"}
    policy.grant_call("execute_command_tool", arguments, ctx)
    assert policy.decide("execute_command_tool", arguments, ctx).action == "ask"
    assert policy.decide("write_file", {"path": "x.txt"}, ctx).action == "deny"


def test_exact_call_grant_stays_in_session(tmp_path):
    policy = Policy(trust_level="ask")
    ctx = context(tmp_path, policy)
    arguments = {"command": "python -m pytest"}
    policy.grant_call("execute_command_tool", arguments, ctx)
    restored = Policy(trust_level="ask", session_grants=set(policy.session_grants))
    assert restored.decide("execute_command_tool", arguments, ctx).action == "allow"
    assert restored.decide("execute_command_tool", {"command": "echo other"}, ctx).action == "ask"


@pytest.mark.parametrize("level", ["read_only", "ask", "full"])
def test_task_queries_and_read_only_delegation(level, tmp_path):
    policy = Policy(trust_level=level)
    ctx = context(tmp_path, policy)
    for name in ("task_status", "task_wait", "task_delivery"):
        assert policy.decide(name, {"task_id": "test"}, ctx).action == "allow"
    assert policy.decide(
        "delegate_to_subagent", {"role": "reviewer", "task": "inspect"}, ctx
    ).action == "allow"
    assert policy.decide(
        "delegate_to_subagent", {"role": "builder", "task": "implement"}, ctx
    ).action == {"read_only": "deny", "ask": "ask", "full": "allow"}[level]


async def test_mcp_builtin_name_collision_is_namespaced_and_requires_approval(tmp_path):
    server = FastMCP("collision")
    calls = []

    @server.tool(name="write_file")
    async def remote_write(path: str, content: str) -> dict:
        calls.append((path, content))
        return {"path": path, "remote": True}

    async with MCPAdapter(server) as adapter:
        original = await adapter.list_tools()
        remote = namespace_mcp_tools(original)
        assert remote[0].name == "mcp__write_file"
        assert remote[0].args_schema == original[0].args_schema
        tools = [write_file, *remote]
        model = Model(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": remote[0].name,
                            "args": {"path": "x.txt", "content": "x"},
                            "id": "remote-1",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        graph = create_agent(
            model,
            tools,
            middleware=[PolicyMiddleware(), build_approval_middleware(tools)],
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "collision"}}
        paused = await graph.ainvoke(
            {"messages": [{"role": "user", "content": "write"}]}, config, context=context(tmp_path)
        )
        assert paused["__interrupt__"] and calls == []
        await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}), config, context=context(tmp_path)
        )
        assert calls == [("x.txt", "x")]
        assert not (tmp_path / "x.txt").exists()


def run_git(root, *arguments):
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


async def test_read_only_git_does_not_execute_external_diff(tmp_path, monkeypatch):
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.name", "Test")
    run_git(tmp_path, "config", "user.email", "test@example.invalid")
    target = tmp_path / "tracked.txt"
    target.write_text("old\n", encoding="utf-8")
    run_git(tmp_path, "add", "tracked.txt")
    run_git(tmp_path, "commit", "-m", "base")
    target.write_text("new\n", encoding="utf-8")
    marker = tmp_path / "external-ran.txt"
    driver = tmp_path / "external.py"
    driver.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8"
    )
    external = f'"{Path(sys.executable).as_posix()}" "{driver.as_posix()}"'
    run_git(tmp_path, "config", "diff.external", external)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", external)
    runtime = ToolRuntime(
        state={},
        context=context(tmp_path),
        config={},
        stream_writer=lambda _: None,
        tool_call_id="git-1",
        store=None,
    )
    result = await git.coroutine(action="diff", runtime=runtime)
    assert result["exit_code"] == 0 and "+new" in result["stdout"]
    assert not marker.exists()


async def test_process_tree_stops_descendant_after_leader_exits(tmp_path):
    marker = tmp_path / "child-finished.txt"
    child = f"import time; from pathlib import Path; time.sleep(1.5); Path({str(marker)!r}).write_text('escaped')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(.2)"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_creation_options(),
    )
    job = attach_process_tree(process)
    try:
        for _ in range(200):
            if process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        assert process.returncode == 0
        await asyncio.wait_for(stop_process_tree(process, job), 1)
        await asyncio.wait_for(process.communicate(), 1)
        await asyncio.sleep(1.6)
        assert not marker.exists()
    finally:
        await stop_process_tree(process, job)
        close_process_tree(job)
