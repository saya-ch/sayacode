"""通过真实 LangGraph 图验证 Web 输入队列、插话和会话控制。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import PrivateAttr

from sayacode.host.application import WebHost
from sayacode.web.app import create_web_app
from tests.support import ContractModel
from tests.web.test_host import _configured_host


class _GateModel(ContractModel):
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def started(self) -> asyncio.Event:
        return self._started

    @property
    def release(self) -> asyncio.Event:
        return self._release

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._started.set()
        await self._release.wait()
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def test_idle_queued_message_enters_native_checkpoint_once(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = ContractModel(responses=[AIMessage(content="收到")])
        app.model_override = model
        thread_id = app.session_id

        receipt = await host.queue_message(thread_id, "请检查", message_id="user-once")
        assert receipt["message_id"] == "user-once"
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)

        snapshot = await host.thread_snapshot(thread_id)
        assert [item["text"] for item in snapshot["messages"] if item["role"] == "human"] == [
            "请检查"
        ]
        assert snapshot["messages"][-1]["text"] == "收到"
        assert snapshot["queued_messages"] == []
        assert model.calls == 1
    finally:
        await host.aclose()


async def test_queue_edit_steer_and_remove_use_same_message_identity(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel()
        thread_id = app.session_id
        await host.runtime.update_thread(thread_id, {"status": "stopped"})

        queued = await host.queue_message(thread_id, "初稿", message_id="user-edit")
        assert queued["status"] == "queued"
        edited = await host.edit_queued_message(thread_id, "user-edit", "修订稿")
        assert edited["text"] == "修订稿"
        removed = await host.queue_message(thread_id, "丢弃", message_id="user-remove")
        await host.remove_queued_message(thread_id, removed["message_id"])
        assert [row["message_id"] for row in await host.queued_messages(thread_id)] == [
            "user-edit"
        ]

        await host.runtime.update_thread(thread_id, {"status": "idle"})
        steered = await host.promote_queued_message(thread_id, "user-edit")
        assert steered["status"] == "pending"
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)
        snapshot = await host.thread_snapshot(thread_id)
        assert [item["text"] for item in snapshot["messages"] if item["role"] == "human"] == [
            "修订稿"
        ]
        assert snapshot["queued_messages"] == []
    finally:
        await host.aclose()


async def test_stopping_main_session_drains_active_child_and_blocks_wake(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = _GateModel(responses=[AIMessage(content="子任务完成")])
        app.model_override = model
        root = app.session_id

        child = await host.spawn_task(root, "reviewer", "检查代码", "审阅", False)
        await asyncio.wait_for(model.started.wait(), timeout=10)
        stopped = await host.stop_session_tree(root)
        assert stopped["child_tasks"] == 1
        model.release.set()
        record = await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        assert record.status == "stopped"
        assert record.auto_wake_suspended is True
        snapshot = await host.thread_snapshot(root)
        assert snapshot["status"] == "stopped"
        assert not any(
            event["type"] == "agent.wake.completed" and event["thread_id"] == root
            for event in host.events._events
        )
    finally:
        await host.aclose()


async def test_stopped_parent_can_reopen_then_resume_queued_child(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel()
        root = app.session_id
        child = await host.spawn_task(root, "reviewer", "先检查", "审阅", False)
        await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)

        await host.stop_session_tree(root)
        assert (await host.thread_snapshot(root))["resume_available"] is True
        queued = await host.queue_message(child["thread_id"], "继续检查")
        assert queued["status"] == "queued"

        reopened = await host.resume_run(root)
        assert reopened == {"run_id": "", "thread_id": root, "status": "idle"}
        assert (await host.thread_snapshot(root))["status"] == "idle"
        assert (await host.tasks.get(child["id"])).status == "stopped"
        await host.task_action(child["id"], "resume", {})
        await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        child_snapshot = await host.thread_snapshot(child["thread_id"])
        assert any(
            item["role"] == "human" and "继续检查" in item["text"]
            for item in child_snapshot["messages"]
        )
        assert child_snapshot["queued_messages"] == []
    finally:
        await host.aclose()


async def test_stopped_parent_blocks_paused_child_approval(tmp_path: Path) -> None:
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
                        {"name": "delete_file", "args": {"path": "sample.txt"}, "id": "delete-child"}
                    ],
                ),
                AIMessage(content="完成"),
            ]
        )
        target = app.workspace / "sample.txt"
        target.write_text("保留", encoding="utf-8")
        root = app.session_id
        child = await host.spawn_task(root, "builder", "删除 sample.txt", "修改文件", False)
        paused = await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        assert paused.status == "paused"
        snapshot = await host.thread_snapshot(child["thread_id"])
        approval = snapshot["pending_approval"]
        assert approval is not None

        await host.stop_session_tree(root)
        with pytest.raises(ValueError, match="所属主会话已停止"):
            await host.decide_approval(
                child["thread_id"], approval["checkpoint_id"], [{"type": "approve"}]
            )
        assert target.read_text(encoding="utf-8") == "保留"
        assert (await host.tasks.get(child["id"])).auto_wake_suspended is True
    finally:
        await host.aclose()


async def test_stopping_main_run_keeps_queued_input_until_explicit_restart(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = _GateModel(responses=[AIMessage(content="当前步骤结束")])
        app.model_override = model
        root = app.session_id

        await host.start_run(root, "原任务")
        await asyncio.wait_for(model.started.wait(), timeout=10)
        await host.queue_message(root, "下一条", message_id="waiting-after-stop")
        await host.stop_session_tree(root)
        model.release.set()
        await asyncio.wait_for(host._runs[root].task, timeout=15)
        snapshot = await host.thread_snapshot(root)
        assert snapshot["status"] == "stopped"
        assert [item["text"] for item in snapshot["queued_messages"]] == ["下一条"]
        assert model.calls == 1
    finally:
        await host.aclose()

    # 重启后仍有停止标记和排队消息；不会由通知自动续跑。
    reopened = await WebHost.open(tmp_path / "workspace", home=tmp_path / "state")
    try:
        snapshot = await reopened.thread_snapshot(root)
        assert snapshot["status"] == "stopped"
        assert [item["text"] for item in snapshot["queued_messages"]] == ["下一条"]
        assert snapshot["resume_available"] is True
        identity = reopened.initial_workspace_id
        assert identity is not None
        app = await reopened._app_for_workspace(identity)
        model = ContractModel()
        app.model_override = model
        app._handles.clear()
        await reopened.resume_run(root)
        if root in reopened._runs:
            await asyncio.wait_for(reopened._runs[root].task, timeout=15)
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)
        assert (await reopened.thread_snapshot(root))["queued_messages"] == []
        assert model.calls == 1
    finally:
        await reopened.aclose()


async def test_new_sessions_keep_their_initial_default_model(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        initial = app.session_id
        assert (await host.runtime.get_thread(initial))["profile_name"] == "test"
        assert (await host.thread_snapshot(initial))["effective_model"] == "test"

        app.config.profiles["alternate"] = replace(
            app.config.profiles["test"], name="alternate", model_id="alternate"
        )
        await app.repository.save(app.config)
        await host.select_profile("alternate")

        assert (await host.thread_snapshot(initial))["effective_model"] == "test"
        _, previous_context = await app._context_for_thread(initial)
        assert previous_context.profile_name == "test"
        await host.set_thread_model(initial, "alternate")
        _, changed_context = await app._context_for_thread(initial)
        assert changed_context.profile_name == "alternate"
        await host.runtime.put_thread(initial, changed_context, status="idle")
        assert (await host.runtime.get_thread(initial))["profile_name"] == "test"
        created = await host.create_session(identity, "另一个会话")
        assert (await host.runtime.get_thread(created["id"]))["profile_name"] == "alternate"
        assert (await host.thread_snapshot(created["id"]))["effective_model"] == "alternate"
    finally:
        await host.aclose()


async def test_thread_model_override_and_child_dispatch_snapshot(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel()
        app.config.profiles["alternate"] = replace(
            app.config.profiles["test"], name="alternate", model_id="alternate"
        )
        await app.repository.save(app.config)
        root = app.session_id

        chosen = await host.set_thread_model(root, "alternate")
        assert (chosen["effective_model"], chosen["model_source"]) == (
            "alternate", "thread"
        )
        child = await host.spawn_task(root, "reviewer", "审阅", "子任务", False)
        await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        inherited = await host.thread_snapshot(child["thread_id"])
        assert (inherited["effective_model"], inherited["model_source"]) == (
            "alternate", "task"
        )
        record = await host.tasks.get(child["id"])
        assert record.profile_snapshot is not None
        assert record.profile_snapshot["name"] == "alternate"

        await host.set_thread_model(root, "test")
        assert (await host.thread_snapshot(root))["effective_model"] == "test"
        assert (await host.thread_snapshot(child["thread_id"]))["effective_model"] == "alternate"
        _, child_context = await app._context_for_thread(child["thread_id"])
        assert child_context.profile_name == "alternate"
        await host.set_thread_model(child["thread_id"], "test")
        overridden = await host.thread_snapshot(child["thread_id"])
        assert overridden["effective_model"] == "test"
        assert overridden["model_source"] == "thread"
        assert record.profile_snapshot["name"] == "alternate"
        with pytest.raises(ValueError, match="请选择"):
            await host.set_thread_model(root, " ")
    finally:
        await host.aclose()


async def test_steered_message_reaches_next_model_step_after_tool_result(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        (app.workspace / "sample.txt").write_text("内容", encoding="utf-8")
        model = _GateModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_file", "args": {"path": "sample.txt"}, "id": "read-1"}
                    ],
                ),
                AIMessage(content="已经纳入补充条件"),
            ]
        )
        app.model_override = model
        root = app.session_id

        await host.start_run(root, "先读取文件")
        await asyncio.wait_for(model.started.wait(), timeout=10)
        queued = await host.queue_message(root, "再检查编码", message_id="steer-one")
        assert queued["status"] == "queued"
        promoted = await host.promote_queued_message(root, "steer-one")
        assert promoted["status"] == "pending"
        model.release.set()
        await asyncio.wait_for(host._runs[root].task, timeout=15)
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)

        assert model.calls == 2
        second_request = model.received[1]
        assert any(isinstance(message, ToolMessage) and message.tool_call_id == "read-1"
                   for message in second_request)
        assert any(isinstance(message, HumanMessage) and message.id == "steer-one"
                   for message in second_request)
        snapshot = await host.thread_snapshot(root)
        assert snapshot["queued_messages"] == []
        completed = next(
            item for item in snapshot["activity"]
            if item["type"] == "tool.completed"
            and item["data"].get("tool_call_id") == "read-1"
        )
        assert completed["data"]["tool_input"]["path"] == "sample.txt"
        assert "内容" in completed["data"]["tool_output"]
    finally:
        await host.aclose()


async def test_uploaded_file_is_bound_to_user_message_and_removed_with_session(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = ContractModel()
        app.model_override = model
        thread_id = app.session_id

        async def chunks():
            yield b"project notes\n"

        attachment = await host.attachments.save_stream(thread_id, "notes.txt", chunks())
        await host.queue_message(
            thread_id, "请读取附件", attachment_ids=[attachment.id], message_id="with-file"
        )
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)
        assert host.attachments.resolve(thread_id, attachment.id).bound_to == "with-file"
        received = model.received[0]
        assert any(
            isinstance(message, HumanMessage)
            and "notes.txt" in str(message.content)
            and attachment.path in str(message.content)
            for message in received
        )
        result = await host.delete_session(thread_id)
        assert result["deleted"] is True
        assert not Path(attachment.path).exists()
    finally:
        await host.aclose()


async def test_failed_tool_keeps_error_status_in_persisted_activity(tmp_path: Path) -> None:
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
                        {"name": "read_file", "args": {"path": "missing.txt"}, "id": "missing-1"}
                    ],
                ),
                AIMessage(content="文件不存在"),
            ]
        )
        root = app.session_id
        await host.start_run(root, "读取不存在的文件")
        await asyncio.wait_for(host._runs[root].task, timeout=15)
        snapshot = await host.thread_snapshot(root)
        error = next(
            item for item in snapshot["activity"]
            if item["data"].get("tool_call_id") == "missing-1"
            and item["type"] in {"tool.failed", "tool.completed"}
        )
        assert error["type"] == "tool.failed"
        assert any(
            item["tool_call_id"] == "missing-1" and item["status"] == "error"
            for item in snapshot["messages"]
        )
    finally:
        await host.aclose()


async def test_web_queue_attachment_model_and_resume_routes_share_one_thread(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = ContractModel()
        app.model_override = model
        thread_id = app.session_id
        await host.runtime.update_thread(
            thread_id, {"status": "stopped", "auto_wake_suspended": True}
        )
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<html></html>", encoding="utf-8")
        web = create_web_app(host, static, host.attachments)
        transport = httpx.ASGITransport(app=web)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            auth = await client.post("/api/auth", json={"token": web.state.launch_token})
            assert auth.status_code == 200
            headers = {"X-CSRF-Token": auth.json()["csrf_token"]}
            base = f"/api/threads/{thread_id}"
            uploaded = await client.post(
                base + "/attachments",
                params={"name": "说明.txt"},
                content="本机内容".encode("utf-8"),
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            assert uploaded.status_code == 201
            file_id = uploaded.json()["id"]
            queued = await client.post(
                base + "/queue",
                json={"message": "读取附件", "attachment_ids": [file_id], "message_id": "web-queued"},
                headers=headers,
            )
            assert queued.status_code == 202
            assert queued.json()["status"] == "queued"
            assert (await client.get(base + "/queue")).json()["messages"][0]["message_id"] == "web-queued"
            chosen = await client.patch(
                base + "/model", json={"profile_name": "test"}, headers=headers
            )
            assert chosen.status_code == 200
            assert chosen.json()["model_source"] == "thread"
            for invalid in (None, ""):
                rejected = await client.patch(
                    base + "/model", json={"profile_name": invalid}, headers=headers
                )
                assert rejected.status_code == 422
            blank = await client.patch(
                base + "/model", json={"profile_name": "   "}, headers=headers
            )
            assert blank.status_code == 400
            resumed = await client.post(base + "/runs/resume", json={}, headers=headers)
            assert resumed.status_code == 202
            if app._wake_runs:
                await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)
            assert (await client.get(base + "/queue")).json()["messages"] == []
            assert any(
                isinstance(message, HumanMessage) and "说明.txt" in str(message.content)
                for message in model.received[0]
            )
            stopped = await client.post(base + "/runs/stop", json={}, headers=headers)
            assert stopped.status_code == 202
            assert (await client.get(base + "/snapshot")).json()["status"] == "stopped"
    finally:
        await host.aclose()


async def test_user_queue_is_not_limited_by_background_notification_budget(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = ContractModel()
        app.model_override = model
        root = app.session_id
        await host.runtime.update_thread(
            root, {"status": "stopped", "auto_wake_suspended": True}
        )
        for number in range(5):
            await host.queue_message(root, f"消息 {number}", message_id=f"queue-{number}")
        await host.resume_run(root)
        for _ in range(10):
            wakes = list(app._wake_runs.values())
            if not wakes:
                break
            await asyncio.wait_for(asyncio.gather(*wakes), timeout=15)
            await asyncio.sleep(0)
        assert (await host.thread_snapshot(root))["queued_messages"] == []
        assert model.calls == 5
    finally:
        await host.aclose()


async def test_reused_provider_tool_call_id_never_relabels_old_activity(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        (app.workspace / "first.txt").write_text("第一个文件", encoding="utf-8")
        (app.workspace / "second.txt").write_text("第二个文件", encoding="utf-8")
        app.model_override = ContractModel(
            responses=[
                AIMessage(content="", tool_calls=[
                    {"name": "read_file", "args": {"path": "first.txt"}, "id": "call_1"}
                ]),
                AIMessage(content="第一轮完成"),
                AIMessage(content="", tool_calls=[
                    {"name": "read_file", "args": {"path": "second.txt"}, "id": "call_1"}
                ]),
                AIMessage(content="第二轮完成"),
            ]
        )
        root = app.session_id
        await host.start_run(root, "第一轮")
        await asyncio.wait_for(host._runs[root].task, timeout=15)
        await host.start_run(root, "第二轮")
        await asyncio.wait_for(host._runs[root].task, timeout=15)
        snapshot = await host.thread_snapshot(root)
        tool_messages = [item for item in snapshot["messages"] if item["role"] == "tool"]
        assert len(tool_messages) == 2
        assert "第一个文件" in tool_messages[0]["text"]
        assert "第二个文件" in tool_messages[1]["text"]
        assert all(item["tool_input"] is None for item in tool_messages)
        call_rows = [
            item for item in snapshot["activity"]
            if item["type"] == "tool.completed" and item["data"].get("tool_call_id") == "call_1"
        ]
        assert len(call_rows) == 2
        assert all("tool_input" not in item["data"] for item in call_rows)
    finally:
        await host.aclose()


async def test_direct_message_runs_before_older_next_turn_queue(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = _GateModel(
            responses=[
                AIMessage(content="第一轮结束"),
                AIMessage(content="插话完成"),
                AIMessage(content="普通队列完成"),
            ]
        )
        app.model_override = model
        root = app.session_id
        await host.start_run(root, "原任务")
        await asyncio.wait_for(model.started.wait(), timeout=10)
        await host.queue_message(root, "先排队的 A", message_id="queue-a")
        await host.queue_message(root, "要直接送达的 B", message_id="queue-b")
        await host.promote_queued_message(root, "queue-b")
        model.release.set()
        await asyncio.wait_for(host._runs[root].task, timeout=15)
        for _ in range(8):
            wakes = list(app._wake_runs.values())
            if not wakes:
                break
            await asyncio.wait_for(asyncio.gather(*wakes), timeout=15)
            await asyncio.sleep(0)
        assert model.calls == 3
        second = [str(item.content) for item in model.received[1] if isinstance(item, HumanMessage)]
        third = [str(item.content) for item in model.received[2] if isinstance(item, HumanMessage)]
        assert "要直接送达的 B" in second
        assert "先排队的 A" not in second
        assert "先排队的 A" in third
        assert (await host.thread_snapshot(root))["queued_messages"] == []
    finally:
        await host.aclose()


async def test_stopped_pending_direct_message_can_resume(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = _GateModel(
            responses=[AIMessage(content="当前步骤"), AIMessage(content="补充已处理")]
        )
        app.model_override = model
        root = app.session_id
        await host.start_run(root, "原任务")
        await asyncio.wait_for(model.started.wait(), timeout=10)
        await host.queue_message(root, "直接补充", message_id="pending-direct")
        await host.promote_queued_message(root, "pending-direct")
        await host.stop_session_tree(root)
        model.release.set()
        await asyncio.wait_for(host._runs[root].task, timeout=15)
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*app._wake_runs.values()), timeout=15)
        snapshot = await host.thread_snapshot(root)
        assert snapshot["status"] == "stopped"
        assert snapshot["resume_available"] is True
        assert snapshot["queued_messages"][0]["status"] == "pending"

        await host.resume_run(root)
        if root in host._runs:
            await asyncio.wait_for(host._runs[root].task, timeout=15)
        for _ in range(5):
            wakes = list(app._wake_runs.values())
            if not wakes:
                break
            await asyncio.wait_for(asyncio.gather(*wakes), timeout=15)
            await asyncio.sleep(0)
        assert (await host.thread_snapshot(root))["queued_messages"] == []
        assert model.calls == 2
    finally:
        await host.aclose()
