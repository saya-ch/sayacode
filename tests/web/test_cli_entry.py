"""普通 CLI 入口无需终端和模型配置即可进入 Web 服务。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sayacode.cli.main import amain, build_parser


@pytest.mark.asyncio
async def test_default_entry_starts_web_without_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[tuple[Path, int, bool]] = []

    async def fake_serve(workspace: Path, *, port: int, open_browser: bool) -> int:
        called.append((workspace, port, open_browser))
        return 0

    monkeypatch.setattr("sayacode.cli.web.serve_web", fake_serve)
    assert await amain(["--workspace", str(tmp_path), "--no-open", "--port", "0"]) == 0
    assert called == [(tmp_path.resolve(), 0, False)]


@pytest.mark.asyncio
async def test_web_entry_rejects_headless_model_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await amain(["--workspace", str(tmp_path), "--profile", "unused"]) == 2
    assert "浏览器" in capsys.readouterr().err


def test_web_port_has_explicit_range() -> None:
    parser = build_parser()
    assert parser.parse_args(["--port", "0"]).port == 0
    with pytest.raises(SystemExit, match="2"):
        parser.parse_args(["--port", "65536"])


@pytest.mark.asyncio
async def test_doctor_uses_typed_application_method(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class App:
        async def _doctor(self, bundle: str) -> dict[str, object]:
            assert bundle == ""
            return {"ok": True, "checks": []}

    code = await amain(
        ["--workspace", str(tmp_path), "--doctor", "--json"],
        app_factory=lambda _: App(),
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "checks": []}
