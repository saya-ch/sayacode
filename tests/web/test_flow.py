"""从真实 HTTP 入口运行原生图，并验证审批和检查点的端到端边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage

from sayacode.web.app import create_web_app
from tests.support import ContractModel
from tests.web.test_host import _configured_host


def _client(tmp_path: Path, host: object) -> tuple[httpx.AsyncClient, object]:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<title>SAYACODE</title>", encoding="utf-8")
    app = create_web_app(host, static)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 45123)),
        base_url="http://127.0.0.1:8765",
    )
    return client, app


async def test_http_run_and_approval_share_the_same_native_checkpoint(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    client, web = _client(tmp_path, host)
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
                AIMessage(content="审批后完成"),
            ]
        )
        target = app.workspace / "sample.txt"
        target.write_text("待处理", encoding="utf-8")
        thread_id = app.session_id
        async with client:
            response = await client.post("/api/auth", json={"token": web.state.launch_token})
            assert response.status_code == 200
            csrf = response.json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            started = await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"message": "删除 sample.txt"},
                headers=headers,
            )
            assert started.status_code == 202
            await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
            paused = await client.get(f"/api/threads/{thread_id}/snapshot")
            assert paused.status_code == 200
            checkpoint = paused.json()["pending_approval"]["checkpoint_id"]
            assert target.exists()
            stale = await client.post(
                f"/api/threads/{thread_id}/approvals",
                json={"checkpoint_id": "stale", "decisions": [{"type": "approve"}]},
                headers=headers,
            )
            assert stale.status_code == 400 and target.exists()
            approved = await client.post(
                f"/api/threads/{thread_id}/approvals",
                json={"checkpoint_id": checkpoint, "decisions": [{"type": "approve"}]},
                headers=headers,
            )
            assert approved.status_code == 202
            await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
            completed = (await client.get(f"/api/threads/{thread_id}/snapshot")).json()
            assert completed["pending_approval"] is None
            assert completed["messages"][-1]["text"] == "审批后完成"
            assert not target.exists()
            assert sum(item["type"] == "tool.completed" for item in host.events._events) == 1
    finally:
        await host.aclose()


async def test_http_child_task_updates_independent_thread_and_parent(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    client, web = _client(tmp_path, host)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel(
            responses=[AIMessage(content="审查完成"), AIMessage(content="已处理审查结论")]
        )
        async with client:
            auth = await client.post("/api/auth", json={"token": web.state.launch_token})
            csrf = auth.json()["csrf_token"]
            spawned = await client.post(
                "/api/tasks",
                json={
                    "parent_thread_id": app.session_id,
                    "role": "reviewer",
                    "prompt": "检查项目入口",
                    "title": "入口审查",
                    "worktree_enabled": False,
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert spawned.status_code == 202
            child = spawned.json()
            assert child["title"] == "入口审查"
            await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
            if app._wake_runs:
                await asyncio.wait_for(
                    asyncio.gather(*list(app._wake_runs.values())), timeout=15
                )
            tasks = (await client.get(f"/api/tasks?workspace_id={identity}")).json()["tasks"]
            assert tasks[0]["thread_id"] == child["thread_id"]
            child_snapshot = (
                await client.get(f"/api/threads/{child['thread_id']}/snapshot")
            ).json()
            assert child_snapshot["messages"][-1]["text"] == "审查完成"
            parent_snapshot = (
                await client.get(f"/api/threads/{app.session_id}/snapshot")
            ).json()
            assert any(item["role"] == "agent_inbox" for item in parent_snapshot["messages"])
            assert parent_snapshot["messages"][-1]["text"] == "已处理审查结论"
    finally:
        await host.aclose()
