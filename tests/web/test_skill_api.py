"""从真实 HTTP 操作检查 Skill 状态、线程作用域和闲时门禁。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import HumanMessage

from sayacode.host.views import message_view
from sayacode.tasks import TASK_NAMESPACE, TaskRecord
from sayacode.web.app import create_web_app
from tests.support import ContractModel
from tests.web.test_host import _configured_host
from tests.web.test_runtime_controls import _GateModel


def _skill(workspace: Path, name: str, body: str) -> None:
    directory = workspace / ".agents" / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试方法\n---\n{body}\n", encoding="utf-8"
    )


def test_official_summary_is_not_a_user_message() -> None:
    summary = HumanMessage(
        content="官方摘要正文", additional_kwargs={"lc_source": "summarization"}
    )
    assert message_view(summary, 0)["role"] == "context_summary"
    assert message_view(summary, 0)["text"] == "官方摘要正文"
    assert message_view(HumanMessage(content="真实用户输入"), 1)["role"] == "human"


async def test_skill_http_state_feedback_and_deactivation(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        model = ContractModel()
        app.model_override = model
        thread_id = app.session_id
        _skill(app.workspace, "review", "只在模型上下文出现的 Skill 正文")
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<title>测试工作台</title>", encoding="utf-8")
        web = create_web_app(host, static)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web, client=("127.0.0.1", 45678)),
            base_url="http://127.0.0.1:8765",
        ) as client:
            auth = await client.post("/api/auth", json={"token": web.state.launch_token})
            headers = {"X-CSRF-Token": auth.json()["csrf_token"]}
            catalog = await client.get(
                f"/api/workspaces/{identity}/skills", params={"thread_id": thread_id}
            )
            assert catalog.status_code == 200
            assert [item["name"] for item in catalog.json()["skills"]] == ["review"]
            assert "active" not in catalog.json()["skills"][0]
            denied = await client.post(f"/api/threads/{thread_id}/skills/review/activate", json={})
            assert denied.status_code == 403
            activated = await client.post(
                f"/api/threads/{thread_id}/skills/review/activate", json={}, headers=headers
            )
            assert activated.json() == {"name": "review", "active": True}
            snapshot = (await client.get(f"/api/threads/{thread_id}/snapshot")).json()
            assert snapshot["active_skills"] == ["review"]
            assert snapshot["pending_steps"] is False
            assert snapshot["messages"] == []
            assert "只在模型上下文出现" not in str(snapshot)
            assert model.calls == 0
            assert any(
                event["type"] == "thread.skills_changed"
                and event["thread_id"] == thread_id
                and event["data"] == {"name": "review", "active": True}
                for event in host.events._events
            )
            removed = await client.post(
                f"/api/threads/{thread_id}/skills/review/deactivate", json={}, headers=headers
            )
            assert removed.json() == {"name": "review", "active": False}
            snapshot = (await client.get(f"/api/threads/{thread_id}/snapshot")).json()
            assert snapshot["active_skills"] == []
            assert snapshot["pending_steps"] is False
            assert model.calls == 0
    finally:
        await host.aclose()


async def test_skill_catalog_uses_selected_child_workspace(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        _skill(app.workspace, "root-review", "主工作区方法")
        child_workspace = tmp_path / "child-workspace"
        _skill(child_workspace, "child-review", "子工作区方法")
        child = TaskRecord(
            task_id="skill-catalog-child",
            thread_id="task-skill-catalog-child",
            parent_thread_id=app.session_id,
            role="builder",
            prompt="读取子工作区",
            workspace=str(app.workspace),
            worktree_enabled=False,
            task_workspace=str(child_workspace),
            status="idle",
        )
        await host.runtime.store.aput(
            TASK_NAMESPACE, child.task_id, child.to_store_dict(), index=False
        )
        assert [item["name"] for item in await host.list_skills(identity)] == ["root-review"]
        assert [item["name"] for item in await host.list_skills(identity, child.thread_id)] == [
            "child-review"
        ]
        another = tmp_path / "another"
        another.mkdir()
        second = await host.create_workspace(str(another), "另一个工作区")
        with pytest.raises(ValueError, match="不属于"):
            await host.list_skills(second["id"], child.thread_id)
    finally:
        await host.aclose()


async def test_registered_run_blocks_manual_context_edits(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    model = _GateModel()
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = model
        _skill(app.workspace, "review", "检查只读上下文")
        thread_id = app.session_id
        await host.start_run(thread_id, "运行中的任务")
        await asyncio.wait_for(model.started.wait(), timeout=10)
        with pytest.raises(ValueError, match="正在运行"):
            await host.activate_skill(thread_id, "review")
        with pytest.raises(ValueError, match="正在运行"):
            await host.deactivate_skill(thread_id, "review")
        with pytest.raises(ValueError, match="正在运行"):
            await host.compact_thread(thread_id)
        assert (await host.thread_snapshot(thread_id))["active_skills"] == []
    finally:
        model.release.set()
        await host.aclose()
