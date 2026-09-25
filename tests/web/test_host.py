"""验证 Web 宿主使用真实图状态，并正确隔离多个浏览器和工作区。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from sayacode.config import Config, ConfigRepository, Profile
from sayacode.host.application import WebHost
from sayacode.host.events import EventHub
from tests.support import ContractModel


async def _configured_host(tmp_path: Path) -> WebHost:
    home = tmp_path / "state"
    repository = ConfigRepository(home)
    await repository.save(
        Config(
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
                    tool_selector_max_tools=None,
                    model_retries=0,
                    tool_retries=0,
                )
            },
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return await WebHost.open(workspace, home=home)


async def test_event_hub_fanout_replay_and_gap() -> None:
    hub = EventHub(replay_limit=2)
    left = hub.subscribe("w1")
    right = hub.subscribe("w1")
    assert (await anext(left))["type"] == "stream.ready"
    assert (await anext(right))["type"] == "stream.ready"
    await hub.publish(event_type="assistant.delta", workspace_id="w1", data={"delta": "A"})
    await hub.publish(event_type="assistant.delta", workspace_id="w2", data={"delta": "B"})
    assert (await anext(left))["data"]["delta"] == "A"
    assert (await anext(right))["data"]["delta"] == "A"
    await left.aclose()
    await right.aclose()
    await hub.publish(event_type="run.completed", workspace_id="w1", data={})
    fresh = hub.subscribe("w1")
    assert (await anext(fresh))["type"] == "stream.ready"
    await hub.publish(event_type="task.running", workspace_id="w1", data={})
    assert (await anext(fresh))["type"] == "task.running"
    await fresh.aclose()
    replay = hub.subscribe("w1", after=2)
    assert (await anext(replay))["type"] == "stream.ready"
    assert [ (await anext(replay))["type"] for _ in range(2) ] == [
        "run.completed",
        "task.running",
    ]
    await replay.aclose()
    await hub.publish(event_type="run.completed", workspace_id="w1", data={})
    missing = hub.subscribe("w1", after=1)
    assert (await anext(missing))["data"]["replay_available"] is False
    assert (await anext(missing))["type"] == "stream.resync_required"
    await missing.aclose()
    restarted = hub.subscribe("w1", after=hub.sequence, instance_id="old-process")
    assert (await anext(restarted))["data"]["replay_available"] is False
    assert (await anext(restarted))["type"] == "stream.resync_required"
    await restarted.aclose()


async def test_unconfigured_host_can_create_workspaces_and_sessions(tmp_path: Path) -> None:
    root = tmp_path / "first"
    root.mkdir()
    host = await WebHost.open(root, home=tmp_path / "home")
    try:
        initial = (await host.list_workspaces())[0]
        assert initial["path"] == str(root.resolve())
        assert (await host.list_sessions(initial["id"]))[0]["id"] == initial["active_session_id"]
        another = tmp_path / "second"
        another.mkdir()
        second = await host.create_workspace(str(another), "第二个项目")
        assert second["name"] == "第二个项目"
        assert len(await host.list_sessions(second["id"])) == 1
        created = await host.create_session(initial["id"], "新任务")
        assert created["title"] == "新任务"
        assert (await host.thread_snapshot(created["id"]))["messages"] == []
        with pytest.raises(ValueError, match="请先.*配置"):
            await host.start_run(created["id"], "执行任务")
        assert host._apps[initial["id"]].runtime is host._apps[second["id"]].runtime
        assert host._apps[initial["id"]].tasks is host._apps[second["id"]].tasks
    finally:
        await host.aclose()


async def test_run_keeps_checkpoint_and_events_after_browser_disconnect(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = ContractModel()
        app.model_override = model
        thread_id = app.session_id
        receipt = await host.start_run(thread_id, "请回答")
        assert receipt["status"] == "running"
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        assert model.calls == 1
        snapshot = await host.thread_snapshot(thread_id)
        assert snapshot["messages"][-1]["text"] == "done"
        events = list(host.events._events)
        assert any(event["type"] == "run.started" for event in events)
        assert any(event["type"] == "model.started" for event in events)
        assert any(event["type"] == "model.completed" for event in events)
        assert any(event["type"] == "run.completed" for event in events)
    finally:
        await host.aclose()

    reopened = await WebHost.open(tmp_path / "workspace", home=tmp_path / "state")
    try:
        snapshot = await reopened.thread_snapshot(thread_id)
        assert snapshot["messages"][-1]["text"] == "done"
    finally:
        await reopened.aclose()


async def test_approval_uses_current_checkpoint_and_executes_once(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "delete_file", "args": {"path": "sample.txt"}, "id": "delete-1"}
                    ],
                ),
                AIMessage(content="完成"),
            ]
        )
        target = app.workspace / "sample.txt"
        target.write_text("原内容", encoding="utf-8")
        thread_id = app.session_id
        await host.start_run(thread_id, "删除 sample.txt")
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        paused = await host.thread_snapshot(thread_id)
        approval = paused["pending_approval"]
        assert approval is not None
        assert target.exists()
        with pytest.raises(RuntimeError, match="待审批"):
            await host.set_trust(thread_id, "full")
        assert (await host.thread_snapshot(thread_id))["trust_level"] == "ask"
        with pytest.raises(ValueError, match="快照已变化"):
            await host.decide_approval(thread_id, "stale", [{"type": "approve"}])
        assert target.exists()
        await host.decide_approval(
            thread_id, approval["checkpoint_id"], [{"type": "approve"}]
        )
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        assert not target.exists()
        events = list(host.events._events)
        assert sum(event["type"] == "tool.started" for event in events) == 1
        assert sum(event["type"] == "tool.completed" for event in events) == 1
        resumed = await host.thread_snapshot(thread_id)
        assert resumed["pending_approval"] is None
        assert resumed["messages"][-1]["text"] == "完成"
    finally:
        await host.aclose()


async def test_workspace_auto_requires_each_shell_approval_without_saved_grants(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        assert host.initial_workspace_id is not None
        app = await host._app_for_workspace(host.initial_workspace_id)
        app.model_override = ContractModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_command_tool",
                            "args": {"command": "echo example"},
                            "id": "shell-1",
                        }
                    ],
                ),
                AIMessage(content="完成"),
            ]
        )
        thread_id = app.session_id
        await host.set_trust(thread_id, "workspace_auto")
        await host.start_run(thread_id, "运行 echo")
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        approval = (await host.thread_snapshot(thread_id))["pending_approval"]
        assert approval is not None
        with pytest.raises(ValueError, match="只有询问档"):
            await host.decide_approval(
                thread_id,
                approval["checkpoint_id"],
                [{"type": "approve"}],
                [{"index": 0, "tool_name": "execute_command_tool"}],
            )
        assert not any(event["type"] == "tool.started" for event in host.events._events)
        await host.decide_approval(thread_id, approval["checkpoint_id"], [{"type": "approve"}])
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        assert (await host.thread_snapshot(thread_id))["pending_approval"] is None
    finally:
        await host.aclose()


async def test_rejected_approval_stays_rejected_after_forbidden_trust_change(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        assert host.initial_workspace_id is not None
        app = await host._app_for_workspace(host.initial_workspace_id)
        target = app.workspace / "must-not-exist.txt"
        app.model_override = ContractModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": target.name, "content": "bad"},
                            "id": "write-1",
                        }
                    ],
                ),
                AIMessage(content="拒绝后继续"),
            ]
        )
        thread_id = app.session_id
        await host.start_run(thread_id, "写入文件")
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        approval = (await host.thread_snapshot(thread_id))["pending_approval"]
        assert approval is not None and not target.exists()
        with pytest.raises(RuntimeError, match="待审批"):
            await host.set_trust(thread_id, "full")
        await host.decide_approval(
            thread_id,
            approval["checkpoint_id"],
            [{"type": "reject", "message": "不要写入"}],
        )
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        assert not target.exists()
        assert (await host.thread_snapshot(thread_id))["pending_approval"] is None
    finally:
        await host.aclose()


async def test_read_only_queued_input_does_not_execute_project_hook(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        assert host.initial_workspace_id is not None
        app = await host._app_for_workspace(host.initial_workspace_id)
        marker = tmp_path / "queued-hook.txt"
        config = app.workspace / ".sayacode" / "hooks.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "command": [
                                    sys.executable,
                                    "-c",
                                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        app.hooks.trust()
        app.hooks.reload()
        await host.set_trust(app.session_id, "read_only")
        await host.queue_message(app.session_id, "检查代码")
        assert not marker.exists()
    finally:
        await host.aclose()


async def test_child_events_keep_child_identity_and_parent_can_continue(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel(responses=[AIMessage(content="子任务完成")])
        parent_thread = app.session_id
        child = await host.spawn_task(parent_thread, "reviewer", "检查代码", "代码审阅", False)
        settled = await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        assert settled.result == "子任务完成"
        if app._wake_runs:
            await asyncio.wait_for(
                asyncio.gather(*list(app._wake_runs.values())), timeout=15
            )
        await asyncio.sleep(0)
        child_events = [
            item
            for item in host.events._events
            if item.get("task_id") == child["id"]
        ]
        assert any(item["type"] == "assistant.delta" for item in child_events)
        assert any(item["type"] == "task.idle" for item in child_events)
        assert all(
            item["thread_id"] == child["thread_id"]
            for item in child_events
            if not item["type"].startswith("agent.wake.")
        )
        assert any(
            item["thread_id"] == parent_thread and item["type"].startswith("agent.wake.")
            for item in child_events
        )
        assert (await host.list_thread_tasks(parent_thread))[0]["title"] == "代码审阅"
        assert (await host.set_trust(child["thread_id"], "read_only"))["trust_level"] == "read_only"
        assert (await host.tasks.get(child["id"])).trust_level == "read_only"
    finally:
        await host.aclose()


async def test_web_migrates_saved_jev_session_to_manual_approval(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        thread_id = app.session_id
        await app.runtime.update_thread(thread_id, {"trust_level": "jev"})
    finally:
        await host.aclose()

    reopened = await WebHost.open(tmp_path / "workspace", home=tmp_path / "state")
    try:
        assert (await reopened.thread_snapshot(thread_id))["trust_level"] == "ask"
        assert (await reopened.runtime.get_thread(thread_id))["trust_level"] == "ask"
        assert await reopened.list_checkpoints(thread_id) == []
        assert any(
            tool["name"] == "read_file" for tool in await reopened.list_thread_tools(thread_id)
        )
    finally:
        await reopened.aclose()
