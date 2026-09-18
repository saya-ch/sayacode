# hooks 缺口上半：触发与状态。

import json
import sys

import lib.core.hooks as hk
from lib.core.hooks import HookRuntime
from lib.core.paths import SayacodePaths


def _user_hooks(data):
    path = SayacodePaths.resolve(create=True).user_hooks
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _rt(tmp_path):
    rt = HookRuntime()
    rt.configure_workspace(tmp_path)
    return rt


class TestTrigger:
    def test_unknown_event(self, tmp_path):
        assert _rt(tmp_path).trigger("ghost-event") is None
        assert _rt(tmp_path).trigger("") is None

    def test_no_hooks(self, tmp_path):
        assert _rt(tmp_path).trigger("SessionStart") is None

    def test_echo_hook(self, tmp_path):
        _user_hooks({"hooks": {"SessionStart": ["echo hi"]}})
        rt = _rt(tmp_path)
        assert len(rt.user_hooks) == 1
        assert rt.trigger("SessionStart") is None
        assert len(rt.audit_log) == 1

    def test_status_warnings(self, tmp_path):
        from lib.core.paths import SayacodePaths as _SP

        proj = _SP.resolve(create=False).project_hooks(tmp_path)
        proj.parent.mkdir(parents=True, exist_ok=True)
        proj.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        rt = _rt(tmp_path)
        assert rt.project_hooks == []
        assert rt.status()["warnings"] != []

    def test_record_trim(self, tmp_path):
        from lib.core.hooks import HookRunResult

        rt = _rt(tmp_path)
        for i in range(201):
            rt._record(HookRunResult(event="e", name="n", source="s", returncode=0, stdout="o", stderr="", blocked=False))
        assert len(rt.audit_log) == 100

    def test_workspace_helpers(self, tmp_path):
        hk.restore_hooks_workspace(None)
        hk.restore_hooks_workspace(tmp_path)
        assert hk.get_hook_status()["workspace"] != ""
        assert isinstance(hk.get_hook_audit_log(), list)
        assert hk.get_hooks_workspace() is not None

    def test_blocking_hook(self, tmp_path):
        _user_hooks({"hooks": {"UserPromptSubmit": [{"command": [sys.executable, "-c", "import sys; sys.exit(1)"], "blocking": True, "name": "gate"}]}})
        rt = _rt(tmp_path)
        out = rt.trigger("UserPromptSubmit", {"input": "hi"})
        assert out is not None and "gate" in out


class TestSanitize:
    def test_variants(self):
        assert hk.sanitize_hook_payload("x", key="api_key") == "***"
        assert hk.sanitize_hook_payload((1, 2)) == [1, 2]
        assert hk.sanitize_hook_payload(123) == 123
        assert hk.sanitize_hook_payload(None) is None
        assert hk.sanitize_hook_payload("x" * 3000).endswith("[truncated]")

        class Big:
            def __str__(self):
                return "y" * 3000

        assert hk.sanitize_hook_payload(Big()).endswith("[truncated]")


class TestRunHook:
    def _cmd(self, **kw):
        from lib.core.hooks import HookCommand

        kw.setdefault("event", "SessionStart")
        kw.setdefault("command", "echo hi")
        kw.setdefault("source", "user")
        kw.setdefault("name", "n")
        kw.setdefault("blocking", False)
        kw.setdefault("timeout", 10)
        return HookCommand(**kw)

    def test_timeout(self, monkeypatch):
        import subprocess as _sp

        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(_sp.TimeoutExpired("cmd", 1)))
        out = hk._run_command_hook(self._cmd(), {}, None)
        assert (out.returncode, out.blocked) == (124, False)

    def test_generic_fail(self, monkeypatch):
        import subprocess as _sp

        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
        out = hk._run_command_hook(self._cmd(), {}, None)
        assert out.returncode == 1 and "failed to run" in out.stderr

    def test_list_command(self, tmp_path):
        out = hk._run_command_hook(self._cmd(command=[sys.executable, "-c", "print('hi')"]), {}, tmp_path)
        assert out.returncode == 0 and "hi" in out.stdout

    def test_truncate(self):
        assert hk._truncate_output("x" * 9000).endswith("omitted]")
        assert hk._truncate_output("short") == "short"

    def test_payload(self):
        out = hk._build_event_payload("SessionStart", None, {"k": "v"})
        assert out["event"] == "SessionStart" and out["payload"]["k"] == "v"


class TestLoad:
    def test_variants(self, tmp_path):
        assert hk._load_hooks_file(tmp_path / "nope.json", source="u") == []
        p = tmp_path / "h.json"
        p.write_text("{broken", encoding="utf-8")
        assert hk._load_hooks_file(p, source="u") == []
        p.write_text("[1]", encoding="utf-8")
        assert hk._load_hooks_file(p, source="u") == []
        p.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        assert hk._load_hooks_file(p, source="u") == []

    def test_entries(self, tmp_path):
        p = tmp_path / "h.json"
        p.write_text(json.dumps({"hooks": {"GhostEvent": ["echo x"], "SessionStart": ["echo a", {"command": ["echo", "b"], "name": "nb", "blocking": True, "timeout": "oops"}, 123, {"command": "  "}, {"command": "echo c", "timeout": 999}]}}), encoding="utf-8")
        hooks = hk._load_hooks_file(p, source="u")
        assert len(hooks) == 3
        assert hooks[1].timeout == 10 and hooks[2].timeout == 30

    def test_extracts(self):
        assert hk._extract_hook_command("echo x") == "echo x"
        assert hk._extract_hook_command(123) is None
        assert hk._extract_hook_command({"command": ["a", 1]}) is None
        assert hk._extract_hook_command({}) is None
        assert hk._extract_hook_name({"name": "n"}, "e", 0) == "n"
        assert hk._extract_hook_name({}, "e", 0) == "e-1"
        assert hk._extract_hook_blocking({"blocking": False}, "UserPromptSubmit") is False
        assert hk._extract_hook_blocking({}, "UserPromptSubmit") is True
        assert hk._extract_hook_blocking({}, "SessionStart") is False
        assert hk._extract_hook_timeout({}) == 10
        assert hk._extract_hook_timeout({"timeout": 0}) == 1
        assert hk._normalize_event("user_prompt_submit") == "UserPromptSubmit"
        assert hk._normalize_event("ghost") is None
    def test_read_missing(self, tmp_path):
        assert hk._read_json_file(tmp_path / "nope.json") == {}
