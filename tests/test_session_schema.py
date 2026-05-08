import json
from pathlib import Path

from lib.core.doctor import render_doctor_report, run_doctor_checks
from lib.core.session import SESSION_SCHEMA_VERSION, SessionManager
from lib.runtime.session_store import (
    load_runtime_managers,
    resolve_workspace_session_id,
    save_runtime_state,
    workspace_session_paths,
    workspace_state_paths,
)
from lib.state import create_app_state


def test_session_save_marks_schema_version(tmp_path):
    session_path = tmp_path / "session.json"
    session = SessionManager()

    assert session.save(str(session_path))

    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == SESSION_SCHEMA_VERSION
    assert SessionManager.load(str(session_path)) is not None


def test_session_load_rejects_legacy_schema(tmp_path):
    session_path = tmp_path / "session.json"
    session_path.write_text(
        json.dumps({
            "session_id": "legacy",
            "created_at": "2026-01-01T00:00:00",
            "last_updated": "2026-01-01T00:00:00",
            "messages": [],
        }),
        encoding="utf-8",
    )

    assert SessionManager.load(str(session_path)) is None


def test_doctor_warns_about_legacy_workspace_session(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    from lib.core.doctor import _workspace_session_dir

    state_dir = _workspace_session_dir(workspace)
    state_dir.mkdir(parents=True)
    (state_dir / "session.json").write_text(
        json.dumps({
            "session_id": "legacy",
            "created_at": "2026-01-01T00:00:00",
            "last_updated": "2026-01-01T00:00:00",
            "messages": [],
        }),
        encoding="utf-8",
    )

    report = render_doctor_report(run_doctor_checks(workspace))

    assert "Session Schema" in report
    assert "legacy session data detected" in report


def test_runtime_does_not_load_or_write_legacy_session_mirror(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    legacy_paths = workspace_state_paths(workspace)
    legacy_paths["dir"].mkdir(parents=True)
    legacy_paths["session"].write_text(
        json.dumps({
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": "legacy",
            "created_at": "2026-01-01T00:00:00",
            "last_updated": "2026-01-01T00:00:00",
            "messages": [],
        }),
        encoding="utf-8",
    )

    session, memory, restored = load_runtime_managers(workspace)

    assert restored is False
    assert session.session_id != "legacy"

    state = create_app_state(
        workspace=workspace,
        model_type="ollama",
        model_config={"model_name": "unit"},
        session_manager=session,
        memory_manager=memory,
    )
    save_runtime_state(state)

    assert legacy_paths["session"].read_text(encoding="utf-8").find('"session_id": "legacy"') >= 0
    assert not legacy_paths["memory"].exists()


def test_workspace_session_paths_reject_path_like_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))

    try:
        workspace_session_paths(tmp_path / "workspace", "../escape")
    except ValueError as exc:
        assert "session_id" in str(exc)
    else:
        raise AssertionError("path-like session ids should be rejected")


def test_resolve_workspace_session_id_rejects_path_like_request(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert resolve_workspace_session_id(workspace, "../escape") is None
