# commands 第三波：散点缺行收敛。

from types import SimpleNamespace


from lib.commands import build_default_command_router
from lib.commands.base import CommandContext
from lib.runtime import RuntimeContext
from lib.runtime.state import UserConfig, create_app_state

from tests.test_commands_full import DummyAgent


def _runtime(tmp_path, with_agent=True):
    # 带完整 app_state 的运行时固件。
    state = create_app_state(tmp_path)
    rt = RuntimeContext(
        workspace=tmp_path,
        model_type="ollama",
        model_name="unit",
        model_config={},
        app_state=state,
    )
    if with_agent:
        rt.attach_agent(DummyAgent())
    return rt


def _dispatch(rt, text):
    return build_default_command_router().dispatch(text, rt)


class _RichSession:
    # 带压缩时间的 session 替身。
    def compact(self, focus=None):
        return True

    def get_compact_info(self):
        return {
            "compact_count": 2,
            "last_compact_time": "2024-01-01T00:00:00",
            "running_tokens": 10,
            "context_budget": 100,
            "model_context_limit": 1000,
            "context_limit_known": True,
            "usage_ratio": 0.01,
            "strategy": "x",
            "needs_compact": False,
        }


class _RichAgent(DummyAgent):
    # 带完整统计的 agent 替身。
    def __init__(self):
        super().__init__()
        self.session = _RichSession()

    def get_stats(self):
        return {
            "session_messages": 3,
            "last_total_tokens": 100,
            "last_prompt_tokens": 70,
            "last_completion_tokens": 30,
            "session_total_tokens": 200,
            "session_prompt_tokens": 150,
            "session_completion_tokens": 50,
        }

    def get_compact_info(self):
        info = super().get_compact_info()
        info.update({"compact_count": 2, "last_compact_time": "2024-01-01T00:00:00"})
        return info


class _McpCount:
    # 带服务计数的 mcp 替身。
    def get_server_count(self):
        return 2


# ── router ─────────────────────────────────────────────────────────────────

class TestRouter:
    def test_non_command(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "hello") is None

    def test_empty(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "") is None
        assert _dispatch(_runtime(tmp_path), "   ") is None

    def test_slashes_only(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "///") is None

    def test_list_routes_sorted(self):
        routes = build_default_command_router().list_routes()
        names = [r.name for r in routes]
        assert names == sorted(names) and len(names) > 10


# ── status/stats 深路径 ────────────────────────────────────────────────────

class TestStatusDeep:
    def test_status_rich(self, tmp_path):
        rt = _runtime(tmp_path)
        rt.attach_agent(_RichAgent())
        rt.mcp = _McpCount()
        assert _dispatch(rt, "/status") is True

    def test_status_no_state(self, tmp_path):
        rt = RuntimeContext(workspace=tmp_path, model_type="o", model_name="u", model_config={})
        assert _dispatch(rt, "/status") is True

    def test_stats_no_agent(self, tmp_path):
        assert _dispatch(_runtime(tmp_path, with_agent=False), "/stats") is True

    def test_analyze_no_agent(self, tmp_path):
        assert _dispatch(_runtime(tmp_path, with_agent=False), "/analyze") is True

    def test_compact_with_time(self, tmp_path):
        rt = _runtime(tmp_path)
        rt.attach_agent(_RichAgent())
        assert _dispatch(rt, "/compact") is True

    def test_history_with_messages(self, tmp_path):
        rt = _runtime(tmp_path)
        rt.app_state.session.add_user_message("hello")
        rt.app_state.session.add_assistant_message("hi there")
        assert _dispatch(rt, "/history") is True


# ── doctor ─────────────────────────────────────────────────────────────────

class TestDoctor:
    def test_blocking(self, tmp_path, monkeypatch):
        import lib.commands.diagnostics as _dg

        monkeypatch.setattr(_dg, "has_failed_checks", lambda checks: True)
        assert _dispatch(_runtime(tmp_path), "/doctor") is True


# ── mode 深路径 ────────────────────────────────────────────────────────────

class TestModeDeep:
    def test_no_state(self, tmp_path):
        rt = RuntimeContext(workspace=tmp_path, model_type="o", model_name="u", model_config={})
        assert _dispatch(rt, "/mode") is True

    def test_unknown_mode(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/mode frobnicate") is True

    def test_switch_saves_user_config(self, tmp_path):
        from lib.core.modes import apply_agent_mode_permissions

        rt = _runtime(tmp_path)
        rt.config_stores["user"] = UserConfig()
        try:
            assert _dispatch(rt, "/mode review") is True
            assert rt.agent.mode == "review"
            assert rt.config_stores["user"].agent_mode == "review"
        finally:
            # mode 规则写在共享会话上，切回 build 防止污染后续测试。
            apply_agent_mode_permissions("build", runtime=None)


# ── symbols ────────────────────────────────────────────────────────────────

class TestSymbolsDeep:
    def test_no_query(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    pass\nclass Bar:\n    pass\n", encoding="utf-8")
        assert _dispatch(_runtime(tmp_path), "/symbols") is True


# ── workspace 深路径 ───────────────────────────────────────────────────────

class TestWorkspaceDeep:
    def test_python_project(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("a==1\n", encoding="utf-8")
        (tmp_path / "m.py").write_text("x\n", encoding="utf-8")
        assert _dispatch(_runtime(tmp_path), "/workspace") is True

    def test_js_project(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert _dispatch(_runtime(tmp_path), "/workspace") is True

    def test_with_messages_and_mcp(self, tmp_path):
        rt = _runtime(tmp_path)
        rt.app_state.session.add_user_message("hi")
        rt.mcp = _McpCount()
        assert _dispatch(rt, "/workspace") is True

    def test_custom_commands_present(self, tmp_path):
        d = tmp_path / ".claude" / "commands"
        d.mkdir(parents=True)
        (d / "demo.md").write_text("# demo\n\nDo things.\n", encoding="utf-8")
        (d / "sub").mkdir()
        (d / "sub" / "deep.md").write_text("# deep\n\nDeep things.\n", encoding="utf-8")
        assert _dispatch(_runtime(tmp_path), "/commands") is True


# ── plan 有表 ──────────────────────────────────────────────────────────────

class TestPlanDeep:
    def test_plan_table(self, tmp_path):
        from lib.core.plans import PlanStore

        rt = _runtime(tmp_path)
        store = PlanStore(tmp_path, rt.app_state.session.session_id)
        store.create("g", ["t"])
        assert _dispatch(rt, "/plan") is True


# ── model 深路径 ───────────────────────────────────────────────────────────

class TestModelDeep:
    def test_no_state(self, tmp_path):
        rt = RuntimeContext(workspace=tmp_path, model_type="o", model_name="u", model_config={})
        assert _dispatch(rt, "/model") is True

    def test_config_no_state(self, tmp_path):
        rt = RuntimeContext(workspace=tmp_path, model_type="o", model_name="u", model_config={})
        assert _dispatch(rt, "/config list") is True

    def test_add_wizard_cancelled(self, tmp_path, monkeypatch):
        import lib.commands.model as _model

        class FakeWizard:
            def __init__(self, *a, **k):
                pass

            def run(self, args):
                return 1

        monkeypatch.setattr(_model, "APIConfigWizardCLI", FakeWizard)
        assert _dispatch(_runtime(tmp_path), "/model add") is True

    def test_use_switch_ok(self, tmp_path, monkeypatch):
        import lib.commands.model as _model

        monkeypatch.setattr(_model.APIConfigManager, "set_current", lambda self, name: True)
        monkeypatch.setattr(_model, "switch_runtime_profile",
                            lambda rt, api_manager=None: SimpleNamespace(ok=True, profile_name="p", error=None))
        assert _dispatch(_runtime(tmp_path), "/model use p") is True

    def test_use_switch_fail(self, tmp_path, monkeypatch):
        import lib.commands.model as _model

        monkeypatch.setattr(_model.APIConfigManager, "set_current", lambda self, name: True)
        monkeypatch.setattr(_model, "switch_runtime_profile",
                            lambda rt, api_manager=None: SimpleNamespace(ok=False, profile_name="p", error="x"))
        assert _dispatch(_runtime(tmp_path), "/model use p") is True
        monkeypatch.setattr(_model, "switch_runtime_profile",
                            lambda rt, api_manager=None: SimpleNamespace(ok=False, profile_name="p", error="no_saved_profile"))
        assert _dispatch(_runtime(tmp_path), "/model use p") is True

    def test_use_real_switch(self, tmp_path, monkeypatch):
        import lib.commands.model as _model

        monkeypatch.setattr(_model.APIConfigManager, "set_current", lambda self, name: True)
        assert _dispatch(_runtime(tmp_path), "/model use p") is True

    def test_test_action(self, tmp_path, monkeypatch):
        import lib.commands.model as _model

        class FakeWizard:
            def __init__(self, *a, **k):
                pass

            def run(self, args):
                return 0

        monkeypatch.setattr(_model, "APIConfigWizardCLI", FakeWizard)
        monkeypatch.setattr(_model, "switch_runtime_profile",
                            lambda rt, api_manager=None: SimpleNamespace(ok=True, profile_name="p", error=None))
        assert _dispatch(_runtime(tmp_path), "/model test p") is True

    def test_format_window(self):
        from lib.commands.model import _format_context_window_value

        assert "tokens" in _format_context_window_value(128000)
        assert _format_context_window_value(None) != ""


# ── permissions/hooks 缺行 ─────────────────────────────────────────────────

class TestAuditRows:
    def test_permissions_audit_rows(self, tmp_path):
        from lib.core.permission_session import _active_runtime

        _active_runtime().check("read_file", {"path": "x"})
        assert _dispatch(_runtime(tmp_path), "/permissions audit") is True

    def test_hooks_audit_rows(self, tmp_path, monkeypatch):
        import lib.commands.hooks as _hooks

        monkeypatch.setattr(_hooks, "get_hook_audit_log",
                            lambda: [{"event": "e", "name": "n", "returncode": 0, "blocked": False}])
        assert _dispatch(_runtime(tmp_path), "/hooks audit") is True


# ── mcp 仪表盘 ─────────────────────────────────────────────────────────────

class TestMcpDashboard:
    def test_active_error_configured(self, tmp_path):
        from lib.commands.mcp import print_mcp_config_dashboard

        (tmp_path / ".mcp.json").write_text(
            json_dumps({"mcpServers": {
                "a": {"command": "srv-a"},
                "b": {"url": "http://b"},
                "c": "plain",
            }}),
            encoding="utf-8",
        )
        print_mcp_config_dashboard(tmp_path, status={
            "active_servers": {"a": {"tools": 2}},
            "errors": {"b": "boom", "trust": "untrusted"},
            "tools": [{"alias": "x", "server": "a", "name": "n"}],
        })

    def test_empty_config(self, tmp_path):
        from lib.commands.mcp import print_mcp_config_dashboard

        print_mcp_config_dashboard(tmp_path)


# ── team 缺行 ──────────────────────────────────────────────────────────────

def json_dumps(obj):
    # 本文件内复用的最小 JSON 序列化。
    import json as _json

    return _json.dumps(obj)


class TestTeamDeep:
    def _handler(self, tmp_path, monkeypatch):
        from lib.commands.team import TeamCommandHandler
        from lib.core.paths import SayacodePaths
        from tests.test_commands_full import FakeManager

        handler = TeamCommandHandler()
        mgr = FakeManager()
        mgr.get_worker_state = lambda wid: SimpleNamespace(
            status=SimpleNamespace(value="running"), worktree="/tmp/wt", branch="b", config={}
        ) if wid == "w1" else None
        handler._supervisors[str(SayacodePaths.resolve().home)] = mgr
        return handler

    def test_object_state(self, tmp_path, monkeypatch):
        rt = _runtime(tmp_path)
        handler = self._handler(tmp_path, monkeypatch)
        assert handler.handle(CommandContext(raw="/team spawn builder x", name="team", args="spawn builder x"), rt) is True

    def test_pending_and_failed(self, tmp_path, monkeypatch):
        from tests.test_commands_full import FakeManager
        from lib.commands.team import TeamCommandHandler
        from lib.core.paths import SayacodePaths

        handler = TeamCommandHandler()
        mgr = FakeManager()
        base_state = {"status": "running"}
        mgr.get_worker_state = lambda wid: base_state if wid in {"pending", "failed1", "delivered"} else None
        mgr.get_result = lambda wid: None if wid == "pending" else ({"ok": True, "response": "r", "worktree": "/tmp/wt", "branch": "b"} if wid == "delivered" else {"ok": False, "error": "boom"})
        mgr.wait = lambda wid, timeout=60.0: None if wid == "pending" else ({"ok": True, "response": "r", "worktree": "/tmp/wt", "branch": "b"} if wid == "delivered" else {"ok": False, "error": "boom"})
        handler._supervisors[str(SayacodePaths.resolve().home)] = mgr
        rt = _runtime(tmp_path)
        assert handler.handle(CommandContext(raw="/team result pending", name="team", args="result pending"), rt) is True
        assert handler.handle(CommandContext(raw="/team result failed1", name="team", args="result failed1"), rt) is True
        assert handler.handle(CommandContext(raw="/team wait pending", name="team", args="wait pending"), rt) is True
        assert handler.handle(CommandContext(raw="/team wait failed1", name="team", args="wait failed1"), rt) is True
        assert handler.handle(CommandContext(raw="/team result delivered", name="team", args="result delivered"), rt) is True
        assert handler.handle(CommandContext(raw="/team wait delivered", name="team", args="wait delivered"), rt) is True

    def test_supervisor_built_from_runtime_tools(self, tmp_path, monkeypatch):
        from lib.commands.team import TeamCommandHandler
        from lib.core.paths import SayacodePaths

        handler = TeamCommandHandler()
        rt = _runtime(tmp_path)
        rt.tools = ["tool-a"]
        assert handler.handle(CommandContext(raw="/team status", name="team", args="status"), rt) is True
        supervisor = handler._supervisors[str(SayacodePaths.resolve().home)]
        assert supervisor._tools == ["tool-a"]

    def test_supervisor_cached_per_home(self, tmp_path, monkeypatch):
        from lib.commands.team import TeamCommandHandler

        handler = TeamCommandHandler()
        rt = _runtime(tmp_path)
        first = handler._supervisor(rt)
        assert first is handler._supervisor(rt)
