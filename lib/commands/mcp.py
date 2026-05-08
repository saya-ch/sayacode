"""MCP slash command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from ..core.mcp_runtime import (
    trust_mcp_workspace,
    untrust_mcp_workspace,
)
from ..custom_commands import load_project_mcp_config
from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import console, print_error, print_info, print_success, print_summary_card
from .base import CommandContext, CommandHandler


@dataclass
class McpCommandHandler(CommandHandler):
    """Inspect, trust, and reload project MCP servers."""

    name: str = "mcp"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        parts = command.raw.strip().split()
        action = parts[1].lower() if len(parts) > 1 else "status"
        agent = runtime.agent

        if action in {"status", "list", "show"}:
            print_mcp_config_dashboard(runtime.workspace, status=_runtime_mcp_status(runtime))
            return True

        if action == "trust":
            path = trust_mcp_workspace(runtime.workspace)
            print_success(tr("mcp.trusted"))
            print_info(tr("common.saved_to", path=path))
            if agent is not None and hasattr(agent, "reload_mcp_tools"):
                tools = agent.reload_mcp_tools()
                runtime.attach_tools(getattr(agent, "tools", []), registry=getattr(runtime, "tool_registry", None))
                print_success(tr("mcp.reloaded", count=len(tools)))
            return True

        if action == "untrust":
            path = untrust_mcp_workspace(runtime.workspace)
            print_success(tr("mcp.untrusted"))
            print_info(tr("common.saved_to", path=path))
            if agent is not None and hasattr(agent, "reload_mcp_tools"):
                agent.reload_mcp_tools()
                runtime.attach_tools(getattr(agent, "tools", []), registry=getattr(runtime, "tool_registry", None))
            return True

        if action == "reload":
            if agent is None or not hasattr(agent, "reload_mcp_tools"):
                tools = []
                if runtime.mcp is not None and hasattr(runtime.mcp, "load_config"):
                    runtime.mcp.load_config()
            else:
                tools = agent.reload_mcp_tools()
            if agent is not None:
                runtime.attach_tools(getattr(agent, "tools", []), registry=getattr(runtime, "tool_registry", None))
            print_success(tr("mcp.reloaded", count=len(tools)))
            print_mcp_config_dashboard(runtime.workspace, status=_runtime_mcp_status(runtime))
            return True

        if action == "tools":
            tools = agent.get_mcp_tool_list() if agent is not None and hasattr(agent, "get_mcp_tool_list") else []
            rows = {
                item.get("alias", ""): f"{item.get('server')}.{item.get('name')}"
                for item in tools[:30]
            }
            print_summary_card(tr("mcp.tools_title"), rows or {tr("common.empty"): tr("permissions.no_audit")})
            return True

        print_error(tr("mcp.usage"))
        return True


def _runtime_mcp_status(runtime: RuntimeContext) -> Dict[str, object]:
    agent = runtime.agent
    if agent is not None and hasattr(agent, "get_mcp_registry"):
        status = agent.get_mcp_registry()
        if isinstance(status, dict):
            return status
    mcp_service = runtime.mcp
    servers = mcp_service.list_servers() if mcp_service is not None and hasattr(mcp_service, "list_servers") else []
    return {
        "active_servers": {},
        "errors": {},
        "tools": [],
        "configured_servers": servers,
    }


def print_mcp_config_dashboard(workspace: Path, status: Dict[str, object] | None = None) -> None:
    """Show project .mcp.json config and runtime status."""
    config_path, config = load_project_mcp_config(workspace)
    servers = config.get("mcpServers", {}) if isinstance(config, dict) else {}
    status = status or {"active_servers": {}, "errors": {}, "tools": []}

    if not servers:
        print_info(tr("mcp.none1", path=config_path))
        print_info(tr("mcp.none2"))
        return

    rows: Dict[str, str] = {}
    for name, server_config in list(servers.items())[:10]:
        command = ""
        if isinstance(server_config, dict):
            command = server_config.get("command") or server_config.get("url") or tr("common.configured_status")
        active = status.get("active_servers", {}).get(name)
        if active:
            rows[name] = tr("mcp.server_active", command=command, count=active.get("tools", 0))
        elif name in status.get("errors", {}):
            rows[name] = tr("mcp.server_error", command=command, error=status["errors"][name])
        else:
            rows[name] = tr("mcp.server_configured", command=command)

    if status.get("errors", {}).get("trust"):
        rows["trust"] = status["errors"]["trust"]

    print_summary_card(
        tr("mcp.config_title"),
        rows,
        subtitle=str(config_path),
        footer=tr("mcp.footer_hint"),
    )
    tools = status.get("tools", [])
    if tools:
        tool_rows = {
            item.get("alias", ""): f"{item.get('server')}.{item.get('name')}"
            for item in tools[:20]
        }
        print_summary_card(tr("mcp.tools_title"), tool_rows, footer=tr("mcp.tools_total", count=len(tools)))
    console.print()


__all__ = ["McpCommandHandler", "print_mcp_config_dashboard"]
