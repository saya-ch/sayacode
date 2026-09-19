# commands 第二波：mcp/permissions/hooks/session/tools/preferences 缺行。

import json


from lib.commands import build_default_command_router
from lib.runtime import RuntimeContext
from lib.runtime.state import create_app_state

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


class _McpService:
    # runtime.mcp 替身：带 load_config 与 list_servers。
    def __init__(self):
        self.loaded = False

    def load_config(self):
        self.loaded = True

    def list_servers(self):
        return ["srv1"]


class _McpAgent(DummyAgent):
    # 带 MCP 注册表的 agent 替身。
    def get_mcp_tool_list(self):
        return [{"alias": "a1", "server": "s", "name": "n"}]

    def get_mcp_registry(self):
        return {"active_servers": {"s": {"tools": 1}}, "errors": {}, "tools": []}


# ── mcp ──────────────────────────────────────────────────────────────────

class TestMcp:
    def test_trust(self, tmp_path):
        rt = _runtime(tmp_path)
        assert _dispatch(rt, "/mcp trust") is True
        assert len(rt.tools) == 0

    def test_untrust(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/mcp untrust") is True

    def test_reload_with_agent(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/mcp reload") is True

    def test_reload_with_service(self, tmp_path):
        rt = _runtime(tmp_path, with_agent=False)
        svc = _McpService()
        rt.mcp = svc
        assert _dispatch(rt, "/mcp reload") is True
        assert svc.loaded is True

    def test_reload_bare(self, tmp_path):
        assert _dispatch(_runtime(tmp_path, with_agent=False), "/mcp reload") is True

    def test_tools_empty(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/mcp tools") is True

    def test_tools_list(self, tmp_path):
        from lib.commands.base import CommandContext
        from lib.commands.mcp import McpCommandHandler

        rt = _runtime(tmp_path)
        rt.attach_agent(_McpAgent())
        handler = McpCommandHandler()
        assert handler.handle(CommandContext(raw="/mcp tools", name="mcp", args="tools"), rt) is True
        assert handler.handle(CommandContext(raw="/mcp", name="mcp", args=""), rt) is True

    def test_usage(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/mcp frobnicate") is True

    def test_dashboard_with_servers(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"s1": {"command": "srv"}, "s2": {"url": "http://x"}}}),
            encoding="utf-8",
        )
        rt = _runtime(tmp_path)
        rt.attach_agent(_McpAgent())
        assert _dispatch(rt, "/mcp") is True

    def test_status_registry_not_dict(self, tmp_path, monkeypatch):

        rt = _runtime(tmp_path)
        agent = DummyAgent()
        agent.get_mcp_registry = lambda: ["oops"]
        rt.attach_agent(agent)
        assert _dispatch(rt, "/mcp") is True


# ── permissions ──────────────────────────────────────────────────────────

class TestPermissions:
    def test_audit_empty(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions audit") is True

    def test_audit_json(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions audit --json") is True

    def test_reset(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions reset") is True

    def test_bad_action(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions frobnicate x") is True

    def test_missing_tool(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions allow") is True

    def test_allow_dangerous_rejected(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions allow delete_file") is True

    def test_allow_ok(self, tmp_path):
        rt = _runtime(tmp_path)
        assert _dispatch(rt, "/permissions allow my_test_tool") is True
        assert _dispatch(rt, "/permissions reset") is True

    def test_deny_ok(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/permissions deny my_test_tool project") is True


# ── hooks ────────────────────────────────────────────────────────────────

class TestHooks:
    def test_audit(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/hooks audit") is True

    def test_trust(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/hooks trust") is True

    def test_untrust(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/hooks untrust") is True

    def test_usage(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/hooks frobnicate") is True


# ── session 深路径 ───────────────────────────────────────────────────────

class TestSessionDeep:
    def test_use_roundtrip(self, tmp_path):
        rt = _runtime(tmp_path)
        assert _dispatch(rt, "/session new first") is True
        first_id = rt.app_state.session.session_id
        assert _dispatch(rt, "/session new second") is True
        assert _dispatch(rt, f"/session use {first_id}") is True
        assert rt.app_state.session.session_id == first_id

    def test_use_prefix(self, tmp_path):
        rt = _runtime(tmp_path)
        assert _dispatch(rt, "/session new first") is True
        first_id = rt.app_state.session.session_id
        assert _dispatch(rt, f"/session use {first_id[:8]}") is True

    def test_rename_ok(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session rename hello") is True

    def test_rename_missing(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session rename") is True

    def test_unknown(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/session frobnicate") is True


# ── tools ────────────────────────────────────────────────────────────────

class TestToolsCmd:
    def test_with_runtime_tools(self, tmp_path):
        from lib.tools.git_tools import git_status

        rt = _runtime(tmp_path)
        rt.attach_tools([git_status, object()])
        assert _dispatch(rt, "/tools") is True

    def test_mcp_group(self, tmp_path):
        class McpTool:
            name = "mcp_demo"
            description = "demo tool\nsecond line"

        rt = _runtime(tmp_path)
        rt.attach_tools([McpTool()])
        assert _dispatch(rt, "/tool") is True

    def test_fallback_catalog(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/tools") is True


# ── preferences 深路径 ───────────────────────────────────────────────────

class TestPrefsDeep:
    def test_lang_set(self, tmp_path):
        from lib.i18n import get_language_preference

        before = get_language_preference()
        try:
            assert _dispatch(_runtime(tmp_path), "/lang en") is True
            assert get_language_preference() == "en"
        finally:
            from lib.i18n import set_language

            set_language(before)

    def test_lang_invalid(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/lang xx") is True

    def test_style_set(self, tmp_path):
        rt = _runtime(tmp_path)
        assert _dispatch(rt, "/style concise") is True
        assert rt.agent.style == "concise"

    def test_style_invalid(self, tmp_path):
        assert _dispatch(_runtime(tmp_path), "/style frobnicate") is True

    def test_style_with_user_store(self, tmp_path):
        from lib.runtime.state import UserConfig

        rt = _runtime(tmp_path)
        rt.config_stores["user"] = UserConfig()
        assert _dispatch(rt, "/style concise") is True
        assert rt.config_stores["user"].prompt_style == "concise"

    def test_lang_variants(self, tmp_path):
        from lib.i18n import get_language_preference, set_language

        assert _dispatch(_runtime(tmp_path), "/lang automatic") is True
        assert _dispatch(_runtime(tmp_path), "/lang zh_CN") is True
        assert _dispatch(_runtime(tmp_path), "/lang en_US") is True
        before = get_language_preference()
        try:
            rt = _runtime(tmp_path)
            from lib.runtime.state import UserConfig

            rt.config_stores["user"] = UserConfig()
            assert _dispatch(rt, "/lang en") is True
            assert rt.config_stores["user"].language == "en"
        finally:
            set_language(before)

    def test_format_helpers(self):
        from lib.commands.conversation import _format_context_limit_from_info, _format_context_usage_ratio
        from lib.commands.runtime_info import _format_context_usage_ratio as _ri_ratio

        unknown = {"context_limit_known": False}
        assert _format_context_usage_ratio(unknown) != ""
        assert _format_context_limit_from_info(unknown) != ""
        assert _ri_ratio(unknown) != ""

    def test_restored_workspace(self, tmp_path):
        rt = _runtime(tmp_path)
        rt.app_state.restored_session = True
        rt.app_state.session.add_user_message("hi")
        assert _dispatch(rt, "/workspace") is True

    def test_prefs_with_user_store(self, tmp_path):
        from lib.runtime.state import UserConfig

        rt = _runtime(tmp_path)
        rt.config_stores["user"] = UserConfig()
        assert _dispatch(rt, "/prefs") is True
        assert _dispatch(rt, "/lang") is True

    def test_settings_toggle_dangerous(self, tmp_path, monkeypatch):
        from lib import theme as _theme

        rt = _runtime(tmp_path)
        old = rt.app_state.confirm_dangerous
        monkeypatch.setattr(_theme.console, "input", lambda *a, **k: "2")
        assert _dispatch(rt, "/settings") is True
        assert rt.app_state.confirm_dangerous != old
