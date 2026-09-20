"""Configured product output limit controls native tool spill files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from langchain.tools import ToolRuntime

from sayacode.tools import read_file
from tests.support import contract_app


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
    app = await contract_app(tmp_path)
    try:
        assert (await app.command("settings"))["output_limit_bytes"] == 64 * 1024
        await app.command("settings", "set output_limit_bytes 2048")
        await app.command("settings", "set shutdown_grace_seconds 2.5")
        assert app._context(app.session_id, app.trust_level).output_limit_bytes == 2048
    finally:
        await app.aclose()
    reopened = await contract_app(tmp_path)
    try:
        settings = await reopened.command("settings")
        assert settings["output_limit_bytes"] == 2048
        assert settings["shutdown_grace_seconds"] == 2.5
    finally:
        await reopened.aclose()
