import json
import os

import lib.core.private_io as private_io
from lib.core.private_io import ensure_private_dir, write_private_json, write_private_text


def _mode(path):
    if os.name == "nt":
        return None
    return path.stat().st_mode & 0o777


def test_write_private_text_creates_parent_and_file(tmp_path):
    target = tmp_path / "state" / "session.txt"

    write_private_text(target, "hello\n")

    assert target.read_text(encoding="utf-8") == "hello\n"
    if os.name != "nt":
        assert _mode(target.parent) == 0o700
        assert _mode(target) == 0o600


def test_write_private_json_writes_valid_json(tmp_path):
    target = tmp_path / "state" / "config.json"

    write_private_json(target, {"ok": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_ensure_private_dir_is_idempotent(tmp_path):
    target = tmp_path / "state"

    ensure_private_dir(target)
    ensure_private_dir(target)

    assert target.is_dir()
    if os.name != "nt":
        assert _mode(target) == 0o700


def test_windows_permission_hardening_invokes_icacls(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")
    calls = []

    monkeypatch.setenv("USERDOMAIN", "DOMAIN")
    monkeypatch.setenv("USERNAME", "saya")
    monkeypatch.setattr(
        private_io.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    private_io._restrict_windows_permissions(target, directory=False)

    assert calls
    command = calls[0][0][0]
    assert command[:3] == ["icacls", str(target), "/inheritance:r"]
    assert "/grant:r" in command
    assert "DOMAIN\\saya:(F)" in command
