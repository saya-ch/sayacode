"""Web 产品操作沿用配置、记忆和项目格式，不依赖旧斜杠命令。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx

from sayacode.host.application import WebHost
from sayacode.host.products import _write_project_server
from sayacode.web.app import create_web_app


async def test_model_profiles_persist_without_key_echo(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "state"
    host = await WebHost.open(workspace, home=home)
    try:
        created = await host.create_profile(
            {
                "name": "first",
                "protocol": "openai_chat_completions",
                "base_url": "https://example.test/v1",
                "api_key": "sk-1234567890abcdef",
                "model_id": "model-one",
                "context_length": 32000,
                "max_output_tokens": 4000,
            }
        )
        assert created["has_api_key"] is True
        assert "api_key" not in created
        assert (await host.list_profiles())["active_profile"] == "first"
        changed = await host.update_profile("first", {"model_id": "model-two", "api_key": None})
        assert changed["model_id"] == "model-two"
        assert changed["has_api_key"] is False
        second = await host.create_profile(
            {
                "name": "second",
                "protocol": "anthropic_messages",
                "base_url": "https://example.test",
                "api_key": "another-secret",
                "model_id": "model-three",
                "context_length": 16000,
                "max_output_tokens": 1000,
            }
        )
        assert second["name"] == "second"
        assert (await host.select_profile("second"))["active_profile"] == "second"
        assert (await host.delete_profile("second"))["active_profile"] == "first"
    finally:
        await host.aclose()
    reopened = await WebHost.open(workspace, home=home)
    try:
        catalog = await reopened.list_profiles()
        assert catalog["active_profile"] == "first"
        assert len(catalog["profiles"]) == 1
        assert catalog["profiles"][0]["has_api_key"] is False
    finally:
        await reopened.aclose()


async def test_project_mcp_write_keeps_existing_shape(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"old": {"command": "old-server"}}, "note": "keep"}),
        encoding="utf-8",
    )
    _write_project_server(path, "new", {"command": "new-server", "args": ["--stdio"]})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["note"] == "keep"
    assert saved["mcpServers"]["old"]["command"] == "old-server"
    assert saved["mcpServers"]["new"]["args"] == ["--stdio"]
    _write_project_server(path, "old", None)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "old" not in saved["mcpServers"]
    assert saved["mcpServers"]["new"]["command"] == "new-server"


async def test_memory_operations_use_current_workspace_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = await WebHost.open(workspace, home=tmp_path / "state")
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        first = await host.remember(identity, "project", "这个项目使用中文注释")
        assert first["scope"] == "project"
        assert first["state"] == "active"
        assert first["pinned"] is False
        pinned = await host.pin_memory(identity, first["id"], True)
        assert pinned["pinned"] is True
        detail = await host.memory_detail(identity, first["id"])
        assert detail["id"] == first["id"]
        assert isinstance(detail["evidence"], list)
        records = await host.list_memory(identity, "project", "中文")
        assert len(records) == 1
        corrected = await host.correct_memory(identity, first["id"], "这个项目全部使用中文注释")
        assert "全部" in corrected["text"]
        confirmed = await host.confirm_memory(identity, first["id"])
        assert confirmed["id"] == first["id"]
        forgotten = await host.forget_memory(identity, first["id"])
        assert forgotten["state"] == "forgotten"
        assert await host.list_memory(identity, "project", "中文") == []
        settings = await host.update_memory_settings(identity, {"enabled": True, "learn": "off"})
        assert settings["enabled"] is True
        assert settings["learn"] == "off"
    finally:
        await host.aclose()


async def test_unconfigured_web_exposes_real_read_only_project_data(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True, text=True)
    (workspace / "module.py").write_text("class Thing:\n    def work(self):\n        pass\n", encoding="utf-8")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>SAYACODE</html>", encoding="utf-8")
    host = await WebHost.open(workspace, home=tmp_path / "state")
    try:
        identity = host.initial_workspace_id
        assert identity is not None
        web = create_web_app(host, static)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web, client=("127.0.0.1", 42000)),
            base_url="http://127.0.0.1:8765",
        ) as client:
            auth = await client.post("/api/auth", json={"token": web.state.launch_token})
            assert auth.status_code == 200
            git = await client.get(f"/api/workspaces/{identity}/git/status")
            assert git.status_code == 200, git.text
            assert "module.py" in git.json()["untracked"]
            symbols = await client.get(f"/api/workspaces/{identity}/symbols?query=Thing")
            assert symbols.status_code == 200, symbols.text
            assert symbols.json()["symbols"][0]["name"] == "Thing"
            analysis = await client.get(f"/api/workspaces/{identity}/analysis")
            assert analysis.status_code == 200, analysis.text
            assert "module.py" not in analysis.json()["text"]  # 结构统计，不复制文件正文。
            doctor = await client.get(f"/api/workspaces/{identity}/doctor")
            assert doctor.status_code == 200, doctor.text
            assert any(item["name"] == "workspace_exists" for item in doctor.json()["checks"])
            mcp = await client.get(f"/api/workspaces/{identity}/mcp")
            assert mcp.status_code == 200
            assert mcp.json()["available_tools"] == []
            csrf = auth.json()["csrf_token"]
            added = await client.post(
                f"/api/workspaces/{identity}/mcp/servers",
                json={
                    "name": "example",
                    "scope": "project",
                    "config": {"command": "does-not-exist", "env": {"API_KEY": "private-value"}},
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert added.status_code == 200, added.text
            assert "private-value" not in added.text
            listed = await client.get(f"/api/workspaces/{identity}/mcp")
            assert listed.json()["servers"][0]["status"] == "untrusted"
            assert "private-value" not in listed.text
            removed = await client.request(
                "DELETE",
                f"/api/workspaces/{identity}/mcp/servers/example",
                params={"scope": "project"},
                json={},
                headers={"X-CSRF-Token": csrf},
            )
            assert removed.status_code == 200, removed.text
            assert (await client.get(f"/api/workspaces/{identity}/mcp")).json()["servers"] == []
    finally:
        await host.aclose()
