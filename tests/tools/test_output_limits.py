"""Configured product output limit controls native tool spill files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from langchain.tools import ToolRuntime

from sayacode.host.application import WebHost
from sayacode.tools import read_file


def runtime(root: Path, limit: int) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=SimpleNamespace(
            workspace=root, output_dir=root / "outputs", output_limit_bytes=limit
        ),
        config={},
        stream_writer=lambda _: None,
        tool_call_id="limit-test",
        store=None,
    )


def test_native_file_output_uses_utf8_byte_limit(tmp_path: Path) -> None:
    data = "汉" * 24_000
    (tmp_path / "large.txt").write_text(data, encoding="utf-8")
    result = read_file.func(path="large.txt", runtime=runtime(tmp_path, 64 * 1024))["content"]
    assert result["bytes"] > 64 * 1024
    assert len(result["preview"].encode("utf-8")) <= 64 * 1024
    stored = tmp_path / "outputs" / result["output_file"]
    assert data in stored.read_text(encoding="utf-8")


async def test_output_limit_and_shutdown_grace_are_persistent_settings(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = await WebHost.open(workspace, home=tmp_path / "state")
    try:
        assert (await host.settings())["output_limit_bytes"] == 64 * 1024
        await host.update_settings({"output_limit_bytes": 2048, "shutdown_grace_seconds": 2.5})
        app = await host._app_for_workspace(str(host.initial_workspace_id))
        assert app._context(app.session_id, app.trust_level).output_limit_bytes == 2048
    finally:
        await host.aclose()
    reopened = await WebHost.open(workspace, home=tmp_path / "state")
    try:
        settings = await reopened.settings()
        assert settings["output_limit_bytes"] == 2048
        assert settings["shutdown_grace_seconds"] == 2.5
    finally:
        await reopened.aclose()
