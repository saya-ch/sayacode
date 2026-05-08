import json

from lib.core.audit import AuditEvent, AuditLogService
from lib.core.doctor import write_support_bundle
from lib.core.paths import ConfigStore, SayacodePaths, StateStore


def test_sayacode_paths_honor_environment_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("SAYACODE_HOME", str(home))

    paths = SayacodePaths.resolve(create=True)
    state = StateStore(paths)
    config = ConfigStore(paths)

    assert paths.home == home.resolve()
    assert paths.user_config == home.resolve() / "user_config.json"
    assert paths.api_configs == home.resolve() / "api_configs.json"
    assert paths.user_permissions == home.resolve() / "permissions.json"
    assert state.workspace_state_dir(workspace).parent == home.resolve() / "sessions"
    assert config.write_json(paths.user_config, {"schema_version": 2}).exists()


def test_audit_log_redacts_sensitive_fields_and_skips_bad_lines(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    service = AuditLogService(path=audit_path)
    audit_path.write_text("{bad json}\n", encoding="utf-8")

    service.append(AuditEvent(
        event_type="permission",
        action="write_file",
        allowed=True,
        details={"api_key": "should-not-leak", "path": "demo.py"},
    ))

    events = service.read_recent(limit=5)
    assert len(events) == 1
    assert events[0]["details"]["api_key"] == "***"
    assert events[0]["details"]["path"] == "demo.py"


def test_support_bundle_is_redacted_and_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    bundle_path = tmp_path / "bundle.json"

    written = write_support_bundle(bundle_path, workspace=tmp_path)
    payload = json.loads(written.read_text(encoding="utf-8"))
    flat = json.dumps(payload, ensure_ascii=False).lower()

    assert payload["schema_version"] == 1
    assert "checks" in payload
    assert "should-not-leak" not in flat
    assert "api_key" not in flat
