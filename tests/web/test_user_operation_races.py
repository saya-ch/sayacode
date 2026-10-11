"""从浏览器用户操作出发验证会话删除、子任务审批与停止的竞态。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from tests.support import ContractModel
from tests.web.test_host import _configured_host


class _ApprovalGateModel(ContractModel):
    """父任务不干扰子任务；子任务第二次模型调用由测试控制。"""

    _child_calls: int = PrivateAttr(default=0)
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _release: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def started(self) -> asyncio.Event:
        return self._started

    @property
    def release(self) -> asyncio.Event:
        return self._release

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        delegated = any(
            isinstance(item, HumanMessage) and str(item.content).startswith("## Delegated task")
            for item in messages
        )
        if delegated and self._child_calls == 1:
            self._started.set()
            await self._release.wait()
        if delegated:
            response = self.responses[min(self._child_calls, len(self.responses) - 1)]
            self._child_calls += 1
        else:
            response = AIMessage(content="父任务保持空闲")
        self.received.append(list(messages))
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


async def _wait_web_run(host, thread_id: str) -> None:
    active = host._runs.get(thread_id)
    if active is not None:
        await asyncio.wait_for(active.task, timeout=15)


@pytest.mark.parametrize("operation", ["rename", "model"])
async def test_session_metadata_update_cannot_resurrect_deleted_session(
    tmp_path: Path, operation: str
) -> None:
    """删除和右侧设置同时发生时，迟到的设置响应不能重建会话。"""

    host = await _configured_host(tmp_path)
    gate = asyncio.Event()
    release = asyncio.Event()
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        thread_id = app.session_id
        original_put = host.runtime.store.aput
        delayed = False

        async def delayed_put(namespace, key, value, *args, **kwargs):
            nonlocal delayed
            marker = (
                value.get("title") == "迟到标题"
                if operation == "rename"
                else value.get("profile_override_name") == "test"
            )
            if namespace == ("threads",) and key == thread_id and marker and not delayed:
                delayed = True
                gate.set()
                await release.wait()
            return await original_put(namespace, key, value, *args, **kwargs)

        host.runtime.store.aput = delayed_put
        update = asyncio.create_task(
            host.rename_thread(thread_id, "迟到标题")
            if operation == "rename"
            else host.set_thread_model(thread_id, "test")
        )
        await asyncio.wait_for(gate.wait(), timeout=10)
        delete = asyncio.create_task(host.delete_session(thread_id))
        await asyncio.sleep(0.05)
        assert not delete.done(), "删除应等待同一会话的设置操作释放会话锁"
        release.set()
        await asyncio.wait_for(update, timeout=10)
        result = await asyncio.wait_for(delete, timeout=10)
        assert result["deleted"] is True
        assert await host.runtime.get_thread(thread_id) is None
        assert thread_id not in [
            row["id"] for row in await host.list_sessions(host.initial_workspace_id)
        ]
    finally:
        release.set()
        await host.aclose()


async def _prepare_approved_child(host, model: _ApprovalGateModel) -> tuple[str, str, str]:
    app = await host._app_for_workspace(host.initial_workspace_id)
    app.model_override = model
    (app.workspace / "delete.txt").write_text("待删除", encoding="utf-8")
    (app.workspace / "read.txt").write_text("停止后不得读取", encoding="utf-8")
    child = await host.spawn_task(app.session_id, "builder", "修改文件", "审批任务", False)
    record = await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
    assert record.status == "paused"
    approval = (await host.thread_snapshot(child["thread_id"]))["pending_approval"]
    assert approval is not None
    await host.decide_approval(
        child["thread_id"], approval["checkpoint_id"], [{"type": "approve"}]
    )
    await asyncio.wait_for(model.started.wait(), timeout=10)
    return app.session_id, child["thread_id"], child["id"]


async def test_stopping_parent_drains_child_approval_run_before_next_tool(tmp_path: Path) -> None:
    """主会话停止必须覆盖 Web 宿主持有的子任务审批运行。"""

    host = await _configured_host(tmp_path)
    model = _ApprovalGateModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delete_file", "args": {"path": "delete.txt"}, "id": "delete"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "read.txt"}, "id": "read"}
                ],
            ),
            AIMessage(content="不应到达的结果"),
        ]
    )
    try:
        parent_id, child_id, task_id = await _prepare_approved_child(host, model)
        await host.stop_session_tree(parent_id)
        model.release.set()
        await _wait_web_run(host, child_id)
        record = await host.tasks.wait(task_id)
        assert record.status == "stopped"
        assert record.auto_wake_suspended is True
        assert not any(
            item["tool_call_id"] == "read"
            for item in (await host.thread_snapshot(child_id))["messages"]
            if item["role"] == "tool"
        )
    finally:
        model.release.set()
        await host.aclose()


async def test_stopping_child_action_drains_its_approval_run(tmp_path: Path) -> None:
    """任务面板单独停止子 Agent 时，也必须停止已转入 WebHost 的审批运行。"""

    host = await _configured_host(tmp_path)
    model = _ApprovalGateModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delete_file", "args": {"path": "delete.txt"}, "id": "delete"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "read.txt"}, "id": "read"}
                ],
            ),
            AIMessage(content="不应到达的结果"),
        ]
    )
    try:
        _, child_id, task_id = await _prepare_approved_child(host, model)
        await host.task_action(task_id, "stop", {})
        model.release.set()
        await _wait_web_run(host, child_id)
        record = await host.tasks.get(task_id)
        assert record.status == "stopped"
        assert not any(
            item["tool_call_id"] == "read"
            for item in (await host.thread_snapshot(child_id))["messages"]
            if item["role"] == "tool"
        )
    finally:
        model.release.set()
        await host.aclose()


async def test_approval_completion_advances_child_turn_once(tmp_path: Path) -> None:
    """审批恢复完成时，审批流和 Web 宿主不能重复递增轮次。"""

    host = await _configured_host(tmp_path)
    model = _ApprovalGateModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delete_file", "args": {"path": "delete.txt"}, "id": "delete"}
                ],
            ),
            AIMessage(content="已完成审批"),
        ]
    )
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        app.model_override = model
        (app.workspace / "delete.txt").write_text("待删除", encoding="utf-8")
        child = await host.spawn_task(app.session_id, "builder", "删除文件", "审批完成", False)
        paused = await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        assert paused.status == "paused"
        assert paused.turn_seq == 1
        approval = (await host.thread_snapshot(child["thread_id"]))["pending_approval"]
        assert approval is not None
        await host.decide_approval(
            child["thread_id"], approval["checkpoint_id"], [{"type": "approve"}]
        )
        model.release.set()
        await _wait_web_run(host, child["thread_id"])
        completed = await host.tasks.get(child["id"])
        assert completed.status == "idle"
        assert completed.turn_seq == 2
    finally:
        model.release.set()
        await host.aclose()
