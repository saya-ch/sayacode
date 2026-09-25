"""子 Agent 暂停或停止时，用户消息保持可见且不会被静默消耗。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from tests.support import ContractModel
from tests.web.test_host import _configured_host


async def test_paused_child_keeps_message_queued_until_approval(tmp_path: Path) -> None:
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
                AIMessage(content="已处理"),
            ]
        )
        (app.workspace / "sample.txt").write_text("保留", encoding="utf-8")
        child = await host.spawn_task(app.session_id, "builder", "删除 sample.txt", "修改文件", False)
        record = await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        assert record.status == "paused"
        # 图把审批暂停记为 interrupted；任务档案才有用户可见的 paused 状态。
        assert (await host.runtime.get_thread(child["thread_id"]))["status"] == "interrupted"

        queued = await host.queue_message(
            child["thread_id"], "先解释你的修改", message_id="paused-child-message"
        )
        assert queued["status"] == "queued"
        assert not any(
            receiver == child["thread_id"] for receiver in app._wake_threads.values()
        )
        with pytest.raises(ValueError, match="正在等待审批"):
            await host.promote_queued_message(child["thread_id"], queued["message_id"])
        assert (await host.queued_messages(child["thread_id"]))[0]["status"] == "queued"
        assert (app.workspace / "sample.txt").read_text(encoding="utf-8") == "保留"

        approval = (await host.thread_snapshot(child["thread_id"]))["pending_approval"]
        assert approval is not None
        await host.decide_approval(
            child["thread_id"], approval["checkpoint_id"], [{"type": "reject"}]
        )
        active = host._runs.get(child["thread_id"])
        if active is not None:
            await asyncio.wait_for(active.task, timeout=15)
        if app._wake_runs:
            await asyncio.wait_for(
                asyncio.gather(*list(app._wake_runs.values()), return_exceptions=True),
                timeout=15,
            )
        # 唤醒协程只负责启动子 Agent；实际图执行由独立任务继续完成。
        resumed = await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        assert resumed.status == "idle"
        snapshot = await host.thread_snapshot(child["thread_id"])
        assert snapshot["queued_messages"] == []
        assert any(
            item["role"] == "human" and item["text"] == "先解释你的修改"
            for item in snapshot["messages"]
        )
    finally:
        await host.aclose()


@pytest.mark.parametrize("status", ["stopped", "interrupted"])
async def test_stopped_child_requires_resume_before_direct_send(
    tmp_path: Path, status: str
) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel(responses=[AIMessage(content="第一轮完成")])
        child = await host.spawn_task(app.session_id, "reviewer", "检查", "审阅", False)
        await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*list(app._wake_runs.values())), timeout=15)
        record = await host.tasks.get(child["id"])
        record.status = status
        await host.tasks.update(record)

        queued = await host.queue_message(
            child["thread_id"], "继续检查", message_id=f"{status}-child-message"
        )
        assert queued["status"] == "queued"
        assert not any(
            receiver == child["thread_id"] for receiver in app._wake_threads.values()
        )
        with pytest.raises(ValueError, match="请先恢复"):
            await host.promote_queued_message(child["thread_id"], queued["message_id"])
    finally:
        await host.aclose()
