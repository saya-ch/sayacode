# commands 全覆盖：经 router 分发，状态变化逐一断言。



from lib.commands import build_default_command_router
from lib.runtime import RuntimeContext
from lib.state import create_app_state


class DummySession:
    # agent.session 替身：压缩相关钩子。
    def compact(self, focus=None):
        return True

    def get_compact_info(self):
        return {
            "compact_count": 0,
            "last_compact_time": None,
            "running_tokens": 10,
            "context_budget": 100,
            "model_context_limit": 1000,
            "context_limit_known": True,
            "usage_ratio": 0.01,
            "strategy": "x",
            "needs_compact": False,
        }


class DummyAgent:
    # 最小 agent 替身：只实现命令层用到的钩子。
    tools = []
    model = None

    def __init__(self):
        self.mode = "build"
        self.style = "standard"
        self.reset_called = False
        self.session = DummySession()

    def set_agent_mode(self, mode):
        self.mode = mode
        return mode

    def set_prompt_style(self, style):
        self.style = style
        return style

    def reset(self):
        self.reset_called = True

    def get_stats(self):
        return {"calls": 1}

    def get_context_summary(self):
        return "ctx-summary"

    def analyze_project(self):
        return "analysis-ok"

    def reload_mcp_tools(self):
        return []

    def get_mcp_registry(self):
        return {}


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


# ── 会话与基础命令 ───────────────────────────────────────────────────────

class TestConversation:
    def test_help(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/help") is True

    def test_clear(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/clear") is True

    def test_history_empty(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/history") is True

    def test_context(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/context") is True

    def test_guide(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/guide") is True

    def test_compact(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/compact") is True

    def test_exit_false(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/exit") is False

    def test_unknown_none(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/nope-cmd") is None


# ── session ──────────────────────────────────────────────────────────────

class TestSession:
    def test_list(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session list") is True

    def test_current(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session current") is True

    def test_help(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session help") is True

    def test_new(self, tmp_path):
        rt = _runtime(tmp_path)
        old_id = rt.app_state.session.session_id
        assert _dispatch(rt, "/session new hello") is True
        assert rt.app_state.session.session_id != old_id

    def test_new_alias(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/new") is True

    def test_use_missing_arg(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session use") is True

    def test_use_unknown(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session use ghost") is True

    def test_no_state(self, tmp_path):
        rt = RuntimeContext(workspace=tmp_path, model_type="o", model_name="u", model_config={})
        assert _dispatch(rt, "/session list") is True


# ── team（fake manager） ─────────────────────────────────────────────────

class FakeManager:
    # TeamSupervisor 替身：覆盖命令层用到的全部方法。
    def __init__(self):
        self.cleaned = 0
        from types import SimpleNamespace

        self.worktrees = SimpleNamespace(
            prepare=lambda worker_id, workspace: SimpleNamespace(
                workspace="/tmp/wt", branch="b", source_commit="c"))

    def attach_worktree(self, *args, **kwargs):
        pass

    def bind_supervisor_context(self, **kw):
        pass

    def spawn(self, agent_type, task, workspace=None, worker_id=None):
        if agent_type == "bad":
            raise ValueError("unknown type")
        return worker_id or "w1"

    def get_worker_state(self, wid):
        if wid != "w1":
            return None
        return {"status": "done", "worktree": "/tmp/wt", "branch": "b", "config": {"branch": "b"}}

    def get_result(self, wid):
        if wid == "pending":
            return None
        return {"ok": True, "response": "done-response", "worktree": "", "branch": ""}

    def wait(self, wid, timeout=60.0):
        return self.get_result(wid)

    def get_delivery(self, wid):
        if wid == "plain":
            return None
        if wid != "w1":
            raise ValueError("unknown")
        return {"branch": "b", "worktree": "/tmp/wt", "status": "", "diff_stat": "", "commits": ""}

    def cleanup(self):
        self.cleaned += 1
        return 2

    def get_status(self):
        return "team-ok"


def _team_runtime(tmp_path, monkeypatch):
    from lib.commands.team import TeamCommandHandler
    from lib.core.paths import SayacodePaths

    rt = _runtime(tmp_path)
    handler = TeamCommandHandler()
    handler._supervisors[str(SayacodePaths.resolve().home)] = FakeManager()
    monkeypatch.setattr(
        "lib.commands.runtime_handlers.build_default_command_router",
        lambda: None,
    ) if False else None
    return rt, handler


def _team(tmp_path, monkeypatch, text):
    from lib.commands.base import CommandContext

    rt, handler = _team_runtime(tmp_path, monkeypatch)
    name, _, args = text.partition(" ")
    return handler.handle(CommandContext(raw=text, name=name.lstrip("/"), args=args), rt)


class TestTeam:
    def test_status(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team status") is True

    def test_spawn_ok(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team spawn builder do-x") is True

    def test_spawn_bad(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team spawn bad do-x") is True

    def test_result_ok(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team result w1") is True

    def test_result_unknown(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team result ghost") is True

    def test_wait_ok(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team wait w1 5") is True

    def test_wait_bad_number(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team wait w1 oops") is True

    def test_wait_unknown(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team wait ghost") is True

    def test_diff_ok(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team diff w1") is True

    def test_diff_none(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team diff plain") is True

    def test_diff_error(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team diff ghost") is True

    def test_cleanup(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team cleanup") is True

    def test_usage(self, tmp_path, monkeypatch):
        assert _team(tmp_path, monkeypatch, "/team frobnicate") is True

    def test_status_helpers(self):
        from lib.commands.team import _branch_of, _status_of, _worktree_of

        assert _status_of(None) == "unknown"
        assert _status_of({}) == "unknown"
        assert _worktree_of(None) == ""
        assert _branch_of(None) == ""
        assert _branch_of({}) == ""


# ── 其余命令冒烟 + 状态断言 ──────────────────────────────────────────────

class TestMiscCommands:
    def test_doctor(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/doctor") is True

    def test_hooks(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/hooks") is True

    def test_mcp(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/mcp") is True

    def test_model(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/model") is True

    def test_config_list(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/config list") is True

    def test_model_list(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/model list") is True

    def test_model_use_missing(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/model use") is True

    def test_model_use_unknown(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/model use ghost") is True

    def test_model_test_missing(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/model test") is True

    def test_model_show(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/model show") is True

    def test_model_unknown(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/model frobnicate") is True

    def test_permissions(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions") is True

    def test_plan(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/plan") is True

    def test_prefs(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/prefs") is True

    def test_style(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/style") is True

    def test_lang(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/lang") is True

    def test_settings_back(self, tmp_path, monkeypatch):
        from lib import theme as _theme

        monkeypatch.setattr(_theme.console, "input", lambda *a, **k: "0")
        assert _dispatch(_runtime(tmp_path), "/settings") is True

    def test_settings_toggle(self, tmp_path, monkeypatch):
        from lib import theme as _theme

        rt = _runtime(tmp_path)
        old = rt.app_state.stream_output
        monkeypatch.setattr(_theme.console, "input", lambda *a, **k: "1")
        assert _dispatch(rt, "/settings") is True
        assert rt.app_state.stream_output != old

    def test_status(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/status") is True

    def test_stats(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/stats") is True

    def test_analyze(self, tmp_path):
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        assert _dispatch(_runtime(tmp_path), "/analyze") is True

    def test_reset_no_agent(self, tmp_path):
        assert _dispatch(_runtime(tmp_path, with_agent=False), "/reset") is True

    def test_reset_confirmed(self, tmp_path, monkeypatch):
        import lib.commands.runtime_info as _ri

        monkeypatch.setattr(_ri, "confirm_action", lambda *a, **k: True)
        rt = _runtime(tmp_path)
        assert _dispatch(rt, "/reset") is True
        assert rt.agent.reset_called is True

    def test_git_menu_back(self, tmp_path, monkeypatch):
        from lib import theme as _theme

        monkeypatch.setattr(_theme.console, "input", lambda *a, **k: "0")
        assert _dispatch(_runtime(tmp_path), "/git") is True

    def test_git_status_tool(self, tmp_path, monkeypatch):
        from lib import theme as _theme
        from lib.tools.git_tools import git_branch, git_log, git_status

        monkeypatch.setattr(_theme.console, "input", lambda *a, **k: "1")
        rt = _runtime(tmp_path)
        rt.attach_tools([git_status, git_log, git_branch])
        assert _dispatch(rt, "/git") is True

    def test_mode_show(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/mode") is True

    def test_mode_switch(self, tmp_path):
        from lib.core.modes import apply_agent_mode_permissions

        rt = _runtime(tmp_path)
        try:
            assert _dispatch(rt, "/mode plan") is True
            assert rt.agent.mode == "plan"
        finally:
            # mode 规则写在共享会话上，切回 build 防止污染后续测试。
            apply_agent_mode_permissions("build", runtime=None)

    def test_symbols(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        assert _dispatch(_runtime(tmp_path), "/symbols foo") is True

    def test_tools(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/tools") is True

    def test_workspace(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/workspace") is True

    def test_paths(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/paths") is True

    def test_custom(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/commands") is True
