# 剩余缺口：commands/mcp、commands/permissions、cli/main、cli/parser、cli/headless


from lib.commands import build_default_command_router
from lib.runtime import RuntimeContext


def _rt(tmp_path, **kw):
    return RuntimeContext(workspace=tmp_path, model_type="o", model_name="u", model_config={}, **kw)


class TestMcp:
    def test_status(self, tmp_path):
        assert build_default_command_router().dispatch("/mcp", _rt(tmp_path)) is True
        assert build_default_command_router().dispatch("/mcp status", _rt(tmp_path)) is True
        assert build_default_command_router().dispatch("/mcp list", _rt(tmp_path)) is True
        assert build_default_command_router().dispatch("/mcp show", _rt(tmp_path)) is True

    def test_trust(self, tmp_path):
        assert build_default_command_router().dispatch("/mcp trust", _rt(tmp_path)) is True
        assert build_default_command_router().dispatch("/mcp trust", _rt(tmp_path, agent=None)) is True

    def test_untrust(self, tmp_path):
        assert build_default_command_router().dispatch("/mcp untrust", _rt(tmp_path)) is True
        assert build_default_command_router().dispatch("/mcp untrust", _rt(tmp_path, agent=None)) is True

    def test_reload(self, tmp_path):
        assert build_default_command_router().dispatch("/mcp reload", _rt(tmp_path)) is True
        assert build_default_command_router().dispatch("/mcp reload", _rt(tmp_path, agent=None)) is True
        assert build_default_command_router().dispatch("/mcp reload", _rt(tmp_path, agent=None, mcp=SimpleMcp())) is True

    def test_tools(self, tmp_path):
        assert build_default_command_router().dispatch("/mcp tools", _rt(tmp_path)) is True
        assert build_default_command_router().dispatch("/mcp tools", _rt(tmp_path, agent=None)) is True

    def test_usage(self, tmp_path):
        assert build_default_command_router().dispatch("/mcp frobnicate", _rt(tmp_path)) is True

    def test_status_helpers(self, tmp_path):
        from lib.commands.mcp import _runtime_mcp_status, print_mcp_config_dashboard
        from types import SimpleNamespace

        assert _runtime_mcp_status(_rt(tmp_path)) is not None
        agent = SimpleNamespace()
        agent.get_mcp_registry = lambda: ["not-dict"]
        assert _runtime_mcp_status(_rt(tmp_path, agent=agent)) is not None
        print_mcp_config_dashboard(tmp_path)
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"s1": {"command": "srv"}, "s2": {"url": "http://x"}}}', encoding="utf-8")
        print_mcp_config_dashboard(tmp_path, status={"active_servers": {"s1": {"tools": 2}}, "errors": {"s2": "boom", "trust": "untrusted"}, "tools": [{"alias": "a", "server": "s", "name": "n"}]})
        print_mcp_config_dashboard(tmp_path, status={"active_servers": {}, "errors": {}, "tools": []})

class SimpleMcp:
    def load_config(self):
        pass

    def list_servers(self):
        return ["srv1"]
