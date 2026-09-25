"""目录选择与会话删除经过真实 Store 和 SQLite checkpoint 验证。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from langgraph.runtime import RunControl

from sayacode.host.application import WebHost, _ActiveRun
from sayacode.tasks.inbox import INBOX_NAMESPACE
from sayacode.tasks.manager import TASK_NAMESPACE
from sayacode.tasks.records import TaskRecord
from sayacode.web.app import create_web_app
from tests.support import ContractModel
from tests.web.test_host import _configured_host


async def test_directory_browser_lists_only_directories_and_requires_absolute_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "工作区 甲"
    root.mkdir()
    child = root / "源代码"
    child.mkdir()
    (root / "说明.txt").write_text("内容", encoding="utf-8")
    host = await WebHost.open(root, home=tmp_path / "state")
    try:
        listing = await host.browse_directories(str(root))
        assert listing["path"] == str(root.resolve())
        assert listing["parent"] == str(tmp_path.resolve())
        assert listing["directories"] == [{"name": "源代码", "path": str(child.resolve())}]
        assert listing["roots"]
        with pytest.raises(ValueError, match="绝对"):
            await host.browse_directories("relative/path")
    finally:
        await host.aclose()


async def test_delete_session_removes_checkpoint_directory_and_mailbox_but_keeps_audit(
    tmp_path: Path,
) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel()
        thread_id = app.session_id
        receipt = await host.start_run(thread_id, "先记录一轮")
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
        assert receipt["status"] == "running"
        assert await host.list_checkpoints(thread_id)
        await app.audit.append("deletion-test", thread_id=thread_id)

        child = TaskRecord(
            task_id="child-for-delete",
            thread_id="task-child-for-delete",
            parent_thread_id=thread_id,
            role="reviewer",
            prompt="完成的只读检查",
            workspace=str(app.workspace),
            worktree_enabled=False,
            status="interrupted",
            unconfirmed_effects=True,
        )
        await app.runtime.store.aput(TASK_NAMESPACE, child.task_id, child.to_store_dict(), index=False)
        grandchild = TaskRecord(
            task_id="nested-child-for-delete",
            thread_id="task-nested-child-for-delete",
            parent_thread_id=child.thread_id,
            role="planner",
            prompt="嵌套检查",
            workspace=str(app.workspace),
            worktree_enabled=False,
            status="completed",
        )
        await app.runtime.store.aput(
            TASK_NAMESPACE, grandchild.task_id, grandchild.to_store_dict(), index=False
        )
        await app.runtime.store.aput(
            INBOX_NAMESPACE,
            "message-for-delete",
            {"sender_thread_id": child.thread_id, "receiver_thread_id": thread_id, "status": "delivered"},
            index=False,
        )

        preview = await host.session_deletion_preview(thread_id)
        assert preview["allowed"] is True
        assert preview["child_task_count"] == 2
        assert "未确认" in preview["warnings"][0]
        result = await host.delete_session(thread_id)
        assert result["deleted"] is True
        assert result["next_session_id"] != thread_id
        assert await app.runtime.get_thread(thread_id) is None
        assert await app.runtime.checkpointer.aget_tuple(app.runtime.thread_config(thread_id)) is None
        assert await app.runtime.store.aget(TASK_NAMESPACE, child.task_id) is None
        assert await app.runtime.store.aget(TASK_NAMESPACE, grandchild.task_id) is None
        assert await app.runtime.store.aget(INBOX_NAMESPACE, "message-for-delete") is None
        assert (await host.list_workspaces())[0]["active_session_id"] == result["next_session_id"]
        assert (await host.list_sessions(identity))[0]["id"] == result["next_session_id"]
        assert (await app.audit.list(thread_id=thread_id))[-1]["event"] == "deletion-test"
    finally:
        await host.aclose()


async def test_delete_session_rejects_unhandled_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = await WebHost.open(workspace, home=tmp_path / "state")
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        thread_id = app.session_id
        child = TaskRecord(
            task_id="builder-for-delete",
            thread_id="task-builder-for-delete",
            parent_thread_id=thread_id,
            role="builder",
            prompt="写代码",
            workspace=str(app.workspace),
            worktree_enabled=True,
            worktree_root=str(tmp_path / "retained-worktree"),
            status="completed",
        )
        await app.runtime.store.aput(TASK_NAMESPACE, child.task_id, child.to_store_dict(), index=False)
        preview = await host.session_deletion_preview(thread_id)
        assert preview["allowed"] is False
        assert "工作树" in preview["blockers"][0]
        with pytest.raises(RuntimeError, match="工作树"):
            await host.delete_session(thread_id)
        assert await app.runtime.get_thread(thread_id) is not None
        assert thread_id not in host._deleting_sessions
    finally:
        await host.aclose()


async def test_delete_session_waits_for_active_main_run(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = await WebHost.open(workspace, home=tmp_path / "state")
    sleeper = asyncio.create_task(asyncio.sleep(60))
    try:
        thread_id = (await host.list_sessions(host.initial_workspace_id or ""))[0]["id"]
        host._runs[thread_id] = _ActiveRun(
            "run-test", thread_id, "now", RunControl(), sleeper, "user"
        )
        assert (await host.session_deletion_preview(thread_id))["allowed"] is False
        with pytest.raises(RuntimeError, match="正在运行"):
            await host.delete_session(thread_id)
        assert await host.runtime.get_thread(thread_id) is not None
    finally:
        host._runs.clear()
        sleeper.cancel()
        await asyncio.gather(sleeper, return_exceptions=True)
        await host.aclose()


async def test_browser_directory_and_delete_routes_use_local_auth(tmp_path: Path) -> None:
    workspace = tmp_path / "浏览目录"
    workspace.mkdir()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>test</html>", encoding="utf-8")
    host = await WebHost.open(workspace, home=tmp_path / "state")
    web = create_web_app(host, static)
    try:
        transport = httpx.ASGITransport(app=web, client=("127.0.0.1", 42000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            assert (await client.get("/api/directories", params={"path": str(workspace)})).status_code == 401
            auth = await client.post("/api/auth", json={"token": web.state.launch_token})
            assert auth.status_code == 200
            csrf = auth.json()["csrf_token"]
            listing = await client.get("/api/directories", params={"path": str(workspace)})
            assert listing.status_code == 200
            assert listing.json()["path"] == str(workspace.resolve())
            session = (await host.list_sessions(host.initial_workspace_id or ""))[0]["id"]
            assert (
                await client.request("DELETE", f"/api/threads/{session}", json={})
            ).status_code == 403
            deleted = await client.request(
                "DELETE", f"/api/threads/{session}", json={}, headers={"X-CSRF-Token": csrf}
            )
            assert deleted.status_code == 200
            assert deleted.json()["deleted"] is True
    finally:
        await host.aclose()
