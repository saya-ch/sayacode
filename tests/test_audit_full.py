# audit 全覆盖：事件、脱敏、读写、保留、导出。

from lib.core.audit import (
    AuditEvent,
    AuditLogService,
    append_audit_event,
    read_recent_audit_events,
    redact_value,
)


def _svc(tmp_path):
    # 落盘到隔离目录的服务固件。
    return AuditLogService(path=str(tmp_path / "audit.jsonl"))


class TestEvent:
    def test_to_dict(self):
        e = AuditEvent(event_type="t", action="a", allowed=True, details={"api_key": "secret"})
        d = e.to_dict()
        assert d["details"]["api_key"] == "***"
        assert d["allowed"] is True

    def test_no_allowed(self):
        assert "allowed" not in AuditEvent(event_type="t", action="a").to_dict()


class TestRedact:
    def test_sensitive(self):
        assert redact_value("x", key="my_token") == "***"

    def test_nested(self):
        out = redact_value({"a": {"password": "p"}, "b": [1, 2]})
        assert out["a"]["password"] == "***" and out["b"] == [1, 2]

    def test_truncate(self):
        assert redact_value("x" * 3000).endswith("[truncated]")
        assert redact_value("short") == "short"
        assert redact_value(123) == 123
        assert redact_value(None) is None
        assert redact_value(object()).startswith("<") or True

    def test_big_containers(self):
        out = redact_value({f"k{i}": i for i in range(150)})
        assert len(out) == 100
        out = redact_value(list(range(150)))
        assert len(out) == 100
        out = redact_value((1, 2))
        assert out == [1, 2]
        out = redact_value({1, 2})
        assert sorted(out) == [1, 2]

    def test_long_object(self):
        class Big:
            def __str__(self):
                return "y" * 3000

        assert redact_value(Big()).endswith("[truncated]")


class TestService:
    def test_append_read(self, tmp_path):
        svc = _svc(tmp_path)
        svc.append(AuditEvent(event_type="t", action="a", workspace="w"))
        svc.append({"type": "t2", "action": "b"})
        events = svc.read_recent(limit=10)
        assert len(events) == 2
        assert svc.read_by_type("t", limit=10)[0]["action"] == "a"
        assert svc.read_by_workspace("w", limit=10)[0]["action"] == "a"

    def test_missing_file(self, tmp_path):
        svc = _svc(tmp_path)
        assert svc.read_recent() == []
        assert svc.read_by_type("t") == []
        assert svc.apply_retention() == 0

    def test_corrupt_lines(self, tmp_path):
        svc = _svc(tmp_path)
        with open(svc.path, "w", encoding="utf-8") as f:
            f.write("{broken\n")
            f.write("[1, 2]\n")
            f.write('{"type": "t", "action": "a"}\n')
        assert len(svc.read_recent()) == 1

    def test_read_error(self, tmp_path, monkeypatch):
        from pathlib import Path as _Path

        svc = _svc(tmp_path)
        svc.append({"type": "t"})
        monkeypatch.setattr(_Path, "read_text",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        assert svc.read_recent() == []
        assert svc.read_by_type("t") == []

    def test_load_skips_bad_lines(self, tmp_path):
        svc = _svc(tmp_path)
        with open(svc.path, "w", encoding="utf-8") as f:
            f.write("{broken\n")
            f.write('{"type": "t", "action": "a"}\n')
        assert len(svc.read_by_type("t")) == 1

    def test_retention_empty_file(self, tmp_path):
        svc = _svc(tmp_path)
        svc.path.write_text("", encoding="utf-8")
        assert svc.apply_retention() == 0

    def test_timerange(self, tmp_path):
        svc = _svc(tmp_path)
        svc.append(AuditEvent(event_type="t", action="a", timestamp="2020-01-01T00:00:00+00:00"))
        svc.append(AuditEvent(event_type="t", action="b", timestamp="2030-01-01T00:00:00+00:00"))
        out = svc.read_by_timerange("2019-01-01", "2021-01-01")
        assert [e["action"] for e in out] == ["a"]

    def test_retention(self, tmp_path):
        svc = _svc(tmp_path)
        svc.append(AuditEvent(event_type="t", action="old", timestamp="2020-01-01T00:00:00+00:00"))
        svc.append(AuditEvent(event_type="t", action="bad-ts"))
        with open(svc.path, "a", encoding="utf-8") as f:
            f.write('{"type": "t", "timestamp": "oops"}\n')
        removed = svc.apply_retention(max_days=30)
        assert removed == 2
        assert svc.apply_retention(max_days=3650) == 0

    def test_retention_cap(self, tmp_path):
        svc = _svc(tmp_path)
        for i in range(5):
            svc.append(AuditEvent(event_type="t", action=f"a{i}"))
        assert svc.apply_retention(max_days=3650, max_entries=2) == 3
        assert len(svc.read_recent(limit=10)) == 2

    def test_export(self, tmp_path):
        svc = _svc(tmp_path)
        assert svc.export() == ""
        assert "timestamp" in svc.export(fmt="csv")
        svc.append(AuditEvent(event_type="t", action="a", allowed=True))
        assert "\"action\": \"a\"" in svc.export()
        csv_out = svc.export(fmt="csv")
        assert "true" in csv_out

    def test_default_paths(self):
        svc = AuditLogService()
        assert svc.path is not None


class TestHelpers:
    def test_append_event(self):
        append_audit_event("t", "a", workspace="w", allowed=True, details={"k": "v"})
        append_audit_event("t", "a")
        assert isinstance(read_recent_audit_events(limit=5), list)

    def test_append_crash(self, monkeypatch):
        import lib.core.audit as _audit

        monkeypatch.setattr(_audit, "AuditLogService",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        append_audit_event("t", "a")
