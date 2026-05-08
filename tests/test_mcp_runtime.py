import json
import sys

from lib.core.mcp_runtime import (
    MCPRuntime,
    call_mcp_tool,
    configure_mcp_workspace,
    get_mcp_status,
    load_mcp_tools,
    shutdown_mcp_runtime,
    trust_mcp_workspace,
)
from lib.core.modes import apply_agent_mode_permissions
from lib.core.permissions import PermissionRuntime, create_permission_runtime, set_permission_confirm_callback


def _write_fake_mcp_server(path):
    path.write_text(
        r'''
import json
import os
import sys

TOOLS = [{
    "name": "echo",
    "description": "Echo one message",
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message to echo"}
        },
        "required": ["message"]
    }
}]

for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": message.get("params", {}).get("protocolVersion"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake", "version": "1.0.0"}
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        arguments = message.get("params", {}).get("arguments", {})
        result = {
            "content": [{"type": "text", "text": os.environ.get("SAYACODE_FAKE_MCP_PREFIX", "echo") + ":" + str(arguments.get("message", ""))}],
            "isError": False
        }
    else:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": "unknown method"}
        }), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''.strip(),
        encoding="utf-8",
    )


def _write_mcp_config(workspace, server_script, env=None):
    (workspace / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "fake": {
                    "command": sys.executable,
                    "args": [str(server_script)],
                    "env": env or {},
                }
            }
        }),
        encoding="utf-8",
    )


def test_mcp_project_config_requires_trust(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    _write_mcp_config(workspace, server_script)
    monkeypatch.setenv("SAYACODE_HOME", str(home))

    configure_mcp_workspace(workspace)
    tools = load_mcp_tools()
    status = get_mcp_status()

    assert tools == []
    assert status["trusted"] is False
    assert "trust" in status["errors"]


def test_mcp_loads_and_calls_stdio_tool_after_trust(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    _write_mcp_config(workspace, server_script)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    set_permission_confirm_callback(lambda request: request.tool_name.startswith("mcp_"))

    try:
        configure_mcp_workspace(workspace)
        trust_mcp_workspace(workspace)
        tools = load_mcp_tools()

        assert [tool.name for tool in tools] == ["mcp_fake_echo"]
        assert call_mcp_tool("mcp_fake_echo", {"message": "hello"}) == "echo:hello"
    finally:
        set_permission_confirm_callback(None)
        shutdown_mcp_runtime()
        apply_agent_mode_permissions("build")


def test_plan_mode_denies_mcp_tools(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    _write_mcp_config(workspace, server_script)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    set_permission_confirm_callback(lambda request: True)

    try:
        configure_mcp_workspace(workspace)
        trust_mcp_workspace(workspace)
        load_mcp_tools()
        apply_agent_mode_permissions("plan")

        result = call_mcp_tool("mcp_fake_echo", {"message": "hello"})

        assert "Permission denied" in result
        assert "mode:plan" in result
    finally:
        set_permission_confirm_callback(None)
        shutdown_mcp_runtime()
        apply_agent_mode_permissions("build")


def test_mcp_tools_are_bound_to_the_runtime_that_loaded_them(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    _write_mcp_config(workspace_one, server_script, env={"SAYACODE_FAKE_MCP_PREFIX": "one"})
    _write_mcp_config(workspace_two, server_script, env={"SAYACODE_FAKE_MCP_PREFIX": "two"})
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    set_permission_confirm_callback(lambda request: request.tool_name.startswith("mcp_"))

    runtime_one = MCPRuntime()
    runtime_two = MCPRuntime()
    try:
        trust_mcp_workspace(workspace_one)
        trust_mcp_workspace(workspace_two)
        runtime_one.configure_workspace(workspace_one)
        runtime_two.configure_workspace(workspace_two)
        tool_one = runtime_one.load_tools()[0]
        tool_two = runtime_two.load_tools()[0]

        assert tool_two.invoke({"message": "hello"}) == "two:hello"
        assert tool_one.invoke({"message": "hello"}) == "one:hello"
    finally:
        set_permission_confirm_callback(None)
        runtime_one.shutdown()
        runtime_two.shutdown()
        shutdown_mcp_runtime()
        apply_agent_mode_permissions("build")


def test_mcp_runtime_uses_explicit_permission_service(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    _write_mcp_config(workspace, server_script)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    set_permission_confirm_callback(lambda request: True)

    runtime = MCPRuntime(permissions=PermissionRuntime())
    try:
        trust_mcp_workspace(workspace)
        runtime.configure_workspace(workspace)
        runtime.load_tools()

        result = runtime.call_tool("mcp_fake_echo", {"message": "hello"})

        assert "Permission required" in result
        assert runtime.permissions.audit_log[-1]["tool"] == "mcp_fake_echo"
    finally:
        set_permission_confirm_callback(None)
        runtime.shutdown()
        shutdown_mcp_runtime()
        apply_agent_mode_permissions("build")


def test_mcp_runtime_reflects_runtime_permission_mode_changes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    _write_mcp_config(workspace, server_script)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    set_permission_confirm_callback(lambda request: True)

    permissions = create_permission_runtime(workspace)
    runtime = MCPRuntime(permissions=permissions)
    try:
        trust_mcp_workspace(workspace)
        runtime.configure_workspace(workspace)
        runtime.load_tools()

        assert runtime.call_tool("mcp_fake_echo", {"message": "hello"}) == "echo:hello"

        permissions.set_session_rules({"mcp_*": "deny"}, source="mode:plan")
        denied = runtime.call_tool("mcp_fake_echo", {"message": "hello"})

        assert "Permission denied" in denied
        assert "mode:plan" in denied
    finally:
        set_permission_confirm_callback(None)
        runtime.shutdown()
        shutdown_mcp_runtime()
        apply_agent_mode_permissions("build")
