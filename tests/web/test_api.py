"""用假宿主验证 HTTP 边界，不启动模型和文件工具。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from sayacode.web.app import create_web_app


class FakeHost:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []
        self.approvals: list[tuple[str, str, list[dict[str, str]], list[dict[str, Any]]]] = []
        self.last_after: int | None = None
        self.last_instance_id: str | None = None
        self.actions: list[str] = []

    async def list_workspaces(self) -> list[dict[str, Any]]:
        return [{"id": "w1", "path": "C:/work", "name": "Work", "api_key": "sk-secret"}]

    async def list_sessions(self, workspace_id: str) -> list[dict[str, Any]]:
        assert workspace_id == "w1"
        return [{"id": "t1", "workspace_id": "w1", "title": "Session", "status": "idle"}]

    async def thread_snapshot(self, thread_id: str) -> dict[str, Any]:
        assert thread_id == "t1"
        return {
            "thread_id": "t1",
            "workspace_id": "w1",
            "title": "Session",
            "status": "paused",
            "messages": [{"id": "m1", "role": "assistant", "text": "Hello"}],
            "todos": [{"id": "todo1", "content": "Read", "status": "pending"}],
            "activity": [{"id": "a1", "type": "tool.started", "tool_name": "read_file"}],
            "active_run": {"run_id": "r0", "started_at": "2026-01-01T00:00:00Z", "status": "paused"},
            "pending_approval": {
                "checkpoint_id": "c1",
                "actions": [{"name": "execute_command", "args": {"api_key": "sk-abcdefabcdefabcdef"}}],
            },
            "tasks": [],
            "profile_snapshot": {"api_key": "SHOULD_NOT_LEAK"},
        }

    async def status(self) -> dict[str, Any]:
        return {"model": "test-model", "protocol": "openai_chat_completions", "secret": "never"}

    async def settings(self) -> dict[str, Any]:
        return {"language": "zh", "default_trust": "ask", "api_key": "never"}

    async def start_run(self, thread_id: str, message: str) -> dict[str, Any]:
        self.runs.append((thread_id, message))
        return {"run_id": "r1", "thread_id": thread_id, "status": "running"}

    async def decide_approval(
        self,
        thread_id: str,
        checkpoint_id: str,
        decisions: list[dict[str, str]],
        grants: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if checkpoint_id != "c1":
            raise ValueError("Checkpoint changed")
        self.approvals.append((thread_id, checkpoint_id, decisions, grants))
        return {"run_id": "r2", "thread_id": thread_id, "status": "running"}

    async def list_tasks(self, workspace_id: str | None) -> list[dict[str, Any]]:
        return [
            {
                "id": "task1",
                "thread_id": "child1",
                "parent_thread_id": "t1",
                "title": "Review",
                "role": "reviewer",
                "status": "running",
                "workspace_id": "w1",
                "profile_snapshot": {"api_key": "NEVER"},
            }
        ]

    async def task_action(self, task_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.actions.append(action)
        return {"status": "running", "message": f"{task_id}:{action}"}

    def subscribe(
        self, workspace_id: str | None, after: int | None, instance_id: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        self.last_after = after
        self.last_instance_id = instance_id

        async def events() -> AsyncIterator[dict[str, Any]]:
            yield {
                "seq": 12,
                "instance_id": "a" * 32,
                "type": "assistant.delta",
                "workspace_id": workspace_id or "w1",
                "thread_id": "t1",
                "data": {
                    "delta": "Hello",
                    "api_key": "sk-abcdefabcdefabcdef",
                    "reasoning_content": "private-thought",
                },
                "profile_snapshot": {"api_key": "NEVER"},
            }

        return events()

    async def list_profiles(self) -> dict[str, Any]:
        return {
            "active_profile": "model1",
            "profiles": [
                {
                    "name": "model1",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://example.test",
                    "model_id": "foo",
                    "context_length": 32000,
                    "max_output_tokens": 4000,
                    "has_api_key": True,
                    "api_key": "sk-abcdefabcdefabcdef",
                }
            ],
        }


def _app(tmp_path: Path) -> tuple[Any, FakeHost]:
    static = tmp_path / "static"
    assets = static / "assets"
    assets.mkdir(parents=True)
    (static / "index.html").write_text("<html><body>WebUI</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("window.app=true", encoding="utf-8")
    host = FakeHost()
    return create_web_app(host, static), host


def _client(app: Any, *, client_ip: str = "127.0.0.1", url: str = "http://127.0.0.1:8765") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(client_ip, 45123)),
        base_url=url,
    )


async def _login(client: httpx.AsyncClient, app: Any) -> str:
    response = await client.post("/api/auth", json={"token": app.state.launch_token})
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


@pytest.mark.asyncio
async def test_local_auth_csrf_and_static(tmp_path: Path) -> None:
    app, host = _app(tmp_path)
    async with _client(app) as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.headers["cache-control"] == "no-store"
        index = await client.get("/workspaces/w1")
        assert "WebUI" in index.text
        assert index.headers["cache-control"] == "no-cache"
        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "immutable" in asset.headers["cache-control"]
        assert (await client.get("/api/workspaces")).status_code == 401
        assert (await client.post("/api/auth", json={"token": "wrong"})).status_code == 401
        csrf = await _login(client, app)
        assert (await client.get("/api/auth")).json()["csrf_token"] == csrf
        assert (await client.get("/api/workspaces")).json()["workspaces"][0]["name"] == "Work"
        assert (await client.post("/api/threads/t1/runs", json={"message": "hello"})).status_code == 403
        run = await client.post(
            "/api/threads/t1/runs",
            json={"message": "hello"},
            headers={"X-CSRF-Token": csrf},
        )
        assert run.status_code == 202
        assert run.json() == {"run_id": "r1", "thread_id": "t1", "status": "running"}
        assert host.runs == [("t1", "hello")]


@pytest.mark.asyncio
async def test_rejects_remote_peer_host_and_origin(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with _client(app, client_ip="192.168.1.7") as remote:
        assert (await remote.get("/api/health")).status_code == 403
    async with _client(app, url="http://rebind.invalid:8765") as rebind:
        assert (await rebind.get("/api/health")).status_code == 403
    async with _client(app) as local:
        bad_origin = await local.post(
            "/api/auth",
            json={"token": app.state.launch_token},
            headers={"Origin": "http://evil.invalid"},
        )
        assert bad_origin.status_code == 403


@pytest.mark.asyncio
async def test_approval_checkpoint_and_projected_snapshot(tmp_path: Path) -> None:
    app, host = _app(tmp_path)
    async with _client(app) as client:
        csrf = await _login(client, app)
        snapshot = (await client.get("/api/threads/t1/snapshot")).json()
        assert snapshot["active_run"]["run_id"] == "r0"
        assert snapshot["activity"][0]["type"] == "tool.started"
        assert snapshot["pending_approval"]["checkpoint_id"] == "c1"
        assert "SHOULD_NOT_LEAK" not in json.dumps(snapshot)
        assert "sk-abcdef" not in json.dumps(snapshot)
        assert snapshot["pending_approval"]["actions"][0]["args"]["api_key"] == "[已移除凭据]"
        bad = await client.post(
            "/api/threads/t1/approvals",
            json={"checkpoint_id": "stale", "decisions": [{"type": "approve"}]},
            headers={"X-CSRF-Token": csrf},
        )
        assert bad.status_code == 400
        assert host.approvals == []
        good = await client.post(
            "/api/threads/t1/approvals",
            json={
                "checkpoint_id": "c1",
                "decisions": [{"type": "approve"}],
                "grants": [{"index": 0, "tool_name": "execute_command"}],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert good.status_code == 202
        assert host.approvals == [
            ("t1", "c1", [{"type": "approve"}], [{"index": 0, "tool_name": "execute_command"}])
        ]


@pytest.mark.asyncio
async def test_sse_last_event_id_and_task_action_allowlist(tmp_path: Path) -> None:
    app, host = _app(tmp_path)
    async with _client(app) as client:
        csrf = await _login(client, app)
        response = await client.get(
            "/api/events?workspace_id=w1",
            headers={"Last-Event-ID": f"{'a' * 32}:11"},
        )
        assert response.status_code == 200
        assert host.last_after == 11
        assert host.last_instance_id == "a" * 32
        assert f"id: {'a' * 32}:12\n" in response.text
        assert "sk-abcdef" not in response.text
        assert "private-thought" not in response.text
        response = await client.get(
            f"/api/events?after={'b' * 32}:9", headers={"Last-Event-ID": f"{'a' * 32}:11"}
        )
        assert response.status_code == 200
        assert host.last_after == 11
        assert host.last_instance_id == "a" * 32
        assert (await client.get("/api/events", headers={"Last-Event-ID": "bad"})).status_code == 400
        assert (await client.get("/api/events?after=9")).status_code == 400
        invalid = await client.post(
            "/api/tasks/task1/erase", json={}, headers={"X-CSRF-Token": csrf}
        )
        assert invalid.status_code == 422
        assert host.actions == []
        accepted = await client.post(
            "/api/tasks/task1/stop", json={}, headers={"X-CSRF-Token": csrf}
        )
        assert accepted.status_code == 200
        assert host.actions == ["stop"]


@pytest.mark.asyncio
async def test_profile_key_never_echoed(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with _client(app) as client:
        await _login(client, app)
        result = await client.get("/api/models")
        assert result.status_code == 200
        assert result.json()["profiles"][0]["has_api_key"] is True
        assert "api_key" not in result.json()["profiles"][0]
        assert "sk-abcdef" not in result.text
