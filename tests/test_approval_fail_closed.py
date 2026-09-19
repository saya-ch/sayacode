# 审批 fail-closed：只有显式布尔批准才算通过，其余一律拒绝。

from types import SimpleNamespace

import pytest

from lib.core.middleware import (
    APPROVAL_MALFORMED,
    APPROVAL_REJECTED,
    SayaPermissionMiddleware,
    parse_approval,
)


class _Permissions:
    """最小权限替身：peek 恒为 ask，记录 grant/blocked 调用。"""

    def __init__(self, action="ask", source="session"):
        self.action = action
        self.source = source
        self.granted = []
        self.blocked = []

    def peek(self, name, args):
        return SimpleNamespace(action=self.action, reason="r", source=self.source)

    def grant_once(self, name):
        self.granted.append(name)

    def record_blocked(self, name, args, source, extra=None):
        self.blocked.append((name, extra))
        return SimpleNamespace(reason="denied")


def _request(name="write_file", args=None, call_id="c1"):
    return SimpleNamespace(tool_call={"name": name, "args": args or {}, "id": call_id})


@pytest.mark.parametrize("answer,expected", [
    (True, True),
    ({"approved": True}, True),
    ({"approved": False}, False),
    ({}, False),
    ({"approved": "yes"}, False),
    ("yes", False),
    ("no", False),
    (1, False),
    (0, False),
    ([], False),
    ([1], False),
    (None, False),
])
def test_only_explicit_boolean_approves(answer, expected):
    """回归：非布尔的真值（"no"、1、非空列表）曾经等价于批准。"""
    approved, _outcome = parse_approval(answer)
    assert approved is expected


def test_malformed_payload_is_denied_and_audited(monkeypatch):
    import lib.core.middleware as mw

    permissions = _Permissions()
    monkeypatch.setattr(mw, "interrupt", lambda payload: "no")
    ran = []
    result = SayaPermissionMiddleware(permissions).wrap_tool_call(
        _request(), lambda request: ran.append(True)
    )
    assert ran == []
    assert str(getattr(result, "content", "")).startswith("⚠️")
    assert permissions.granted == []
    assert permissions.blocked[-1][1] == {"approval": APPROVAL_MALFORMED}


def test_explicit_true_runs_the_tool(monkeypatch):
    import lib.core.middleware as mw

    permissions = _Permissions()
    monkeypatch.setattr(mw, "interrupt", lambda payload: {"approved": True})
    sentinel = object()
    outcome = SayaPermissionMiddleware(permissions).wrap_tool_call(
        _request(), lambda request: sentinel
    )
    assert outcome is sentinel
    assert permissions.granted == ["write_file"]
    assert permissions.blocked == []


def test_false_is_rejected_not_malformed(monkeypatch):
    import lib.core.middleware as mw

    permissions = _Permissions()
    monkeypatch.setattr(mw, "interrupt", lambda payload: {"approved": False})
    SayaPermissionMiddleware(permissions).wrap_tool_call(_request(), lambda request: None)
    assert permissions.blocked[-1][1] == {"approval": APPROVAL_REJECTED}


def test_granted_tool_skips_the_interrupt(monkeypatch):
    import lib.core.middleware as mw

    permissions = _Permissions(action="allow")
    monkeypatch.setattr(mw, "interrupt", lambda payload: pytest.fail("不该弹窗"))
    middleware = SayaPermissionMiddleware(permissions)
    assert middleware.wrap_tool_call(_request(), lambda request: "done") == "done"


def test_record_blocked_carries_extra_into_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    from lib.core.audit import AuditLogService
    from lib.core.permission_session import (
        PermissionRuntime,
        SessionPermissionState,
    )

    runtime = PermissionRuntime(session=SessionPermissionState())
    runtime.record_blocked(
        "write_file", {"path": "a"}, "session", extra={"approval": "malformed"}
    )
    events = [e for e in AuditLogService().read_recent(50)
              if e["type"] == "permission"]
    assert events and events[-1]["details"]["approval"] == "malformed"
