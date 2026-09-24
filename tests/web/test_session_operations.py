"""从 Web 路由验证检查点、工具目录和 Hook 仍走现有领域实现。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from sayacode.config import ConfigRepository
from sayacode.host.application import WebHost
from sayacode.web.app import create_web_app
from tests.support import ContractModel
from tests.web.test_host import _configured_host


async def test_session_operations_route_to_native_state_and_hooks(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel()
        thread_id = app.session_id
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<title>SAYACODE</title>", encoding="utf-8")
        web = create_web_app(host, static)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web, client=("127.0.0.1", 45123)),
            base_url="http://127.0.0.1:8765",
        ) as client:
            response = await client.post("/api/auth", json={"token": web.state.launch_token})
            csrf = response.json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            run = await client.post(
                f"/api/threads/{thread_id}/runs", json={"message": "检查项目"}, headers=headers
            )
            assert run.status_code == 202
            await asyncio.wait_for(host._runs[thread_id].task, timeout=15)

            checkpoints = (await client.get(f"/api/threads/{thread_id}/checkpoints")).json()
            assert checkpoints["checkpoints"]
            trace = (await client.get(f"/api/threads/{thread_id}/trace")).json()
            assert any(item["event"] == "model.completed" for item in trace["activity"])
            tools = (await client.get(f"/api/threads/{thread_id}/tools")).json()
            assert any(item["name"] == "read_file" for item in tools["tools"])
            assert any(item["name"] == "write_todos" for item in tools["tools"])
            memory = (await client.get(f"/api/threads/{thread_id}/memory/settings")).json()
            assert memory["thread_id"] == thread_id
            assert memory["learn"] == "off"
            changed = await client.patch(
                f"/api/threads/{thread_id}/memory/settings",
                json={"use": False, "learn": "explicit"},
                headers=headers,
            )
            assert changed.status_code == 200
            assert changed.json()["use_override"] is False
            assert changed.json()["learn_override"] == "explicit"

            bad = await client.post(
                f"/api/threads/{thread_id}/rewind",
                json={"checkpoint_id": "missing"},
                headers=headers,
            )
            assert bad.status_code == 404
            compact = await client.post(
                f"/api/threads/{thread_id}/compact",
                json={"focus": "工作区入口"},
                headers=headers,
            )
            assert compact.status_code == 200
            assert isinstance(compact.json()["compacted"], bool)
            if compact.json()["compacted"]:
                assert any(item["type"] == "thread.compacted" for item in host.events._events)
            chosen = checkpoints["checkpoints"][0]["checkpoint_id"]
            rewound = await client.post(
                f"/api/threads/{thread_id}/rewind",
                json={"checkpoint_id": chosen},
                headers=headers,
            )
            assert rewound.status_code == 200
            assert rewound.json()["rewound"] is True
            assert any(item["type"] == "thread.rewound" for item in host.events._events)

            hooks = (await client.get(f"/api/workspaces/{identity}/hooks")).json()
            assert hooks["project_trusted"] is False
            trusted = await client.patch(
                f"/api/workspaces/{identity}/hooks/trust",
                json={"trusted": True},
                headers=headers,
            )
            assert trusted.status_code == 200
            assert trusted.json()["project_trusted"] is True
            cleared = await client.request(
                "DELETE",
                f"/api/threads/{thread_id}/approvals/grants",
                json={},
                headers=headers,
            )
            assert cleared.status_code == 200
            assert cleared.json()["cleared"] is True
    finally:
        await host.aclose()


async def test_session_panels_remain_readable_before_model_setup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = await WebHost.open(workspace, home=tmp_path / "state")
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<title>SAYACODE</title>", encoding="utf-8")
        web = create_web_app(host, static)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web, client=("127.0.0.1", 45123)),
            base_url="http://127.0.0.1:8765",
        ) as client:
            auth = await client.post("/api/auth", json={"token": web.state.launch_token})
            assert auth.status_code == 200
            base = f"/api/threads/{app.session_id}"
            for path in ("checkpoints", "trace", "tools", "memory/settings"):
                response = await client.get(f"{base}/{path}")
                assert response.status_code == 200, (path, response.text)
            assert (await client.get(f"{base}/checkpoints")).json() == {"checkpoints": []}
    finally:
        await host.aclose()


async def test_removed_model_still_allows_checkpoint_history_read(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        app = await host._app_for_workspace(identity)
        app.model_override = ContractModel()
        thread_id = app.session_id
        await host.start_run(thread_id, "保存一轮历史")
        await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
    finally:
        await host.aclose()

    repository = ConfigRepository(tmp_path / "state")
    config = await repository.load()
    config.default_profile = None
    config.profiles.clear()
    await repository.save(config)

    reopened = await WebHost.open(tmp_path / "workspace", home=tmp_path / "state")
    try:
        checkpoints = await reopened.list_checkpoints(thread_id)
        assert checkpoints
        assert max(item["message_count"] for item in checkpoints) >= 2
        assert (await reopened.thread_snapshot(thread_id))["messages"][-1]["text"] == "done"
    finally:
        await reopened.aclose()
