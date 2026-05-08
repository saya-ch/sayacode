"""Workspace and local-path slash commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..api_config import APIConfigManager
from ..core.context import ProjectContext
from ..core.modes import agent_mode_label
from ..custom_commands import list_custom_commands
from ..i18n import on_off, tr
from ..runtime import RuntimeContext, workspace_state_paths
from ..state import UserConfig
from ..theme import console, print_info, print_summary_card
from .base import CommandContext, CommandHandler


@dataclass
class WorkspaceCommandHandler(CommandHandler):
    name: str = "workspace"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        print_workspace_dashboard(runtime.app_state, runtime.mcp)
        return True


@dataclass
class PathsCommandHandler(CommandHandler):
    name: str = "paths"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        print_local_paths_dashboard(runtime.workspace)
        return True


@dataclass
class CustomCommandsCommandHandler(CommandHandler):
    name: str = "commands"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        print_custom_commands_dashboard(runtime.workspace)
        return True


def format_workspace_snapshot(context: Optional[ProjectContext], workspace: Path) -> tuple[Dict[str, str], Dict[str, str]]:
    """Build workspace summary rows and starter suggestions."""
    ctx = context or ProjectContext(str(workspace))
    file_count = len(ctx.files)
    top_files = ", ".join(file.path for file in ctx.files[:4]) if ctx.files else tr("workspace.no_files")

    workspace_rows = {
        tr("workspace.path"): str(workspace),
        tr("workspace.files"): str(file_count),
        tr("workspace.project_type"): ctx.project_type or "generic",
        tr("workspace.language"): ctx.language or "Unknown",
        tr("workspace.git"): tr("workspace.git_repo") if (workspace / ".git").exists() else tr("workspace.git_none"),
    }

    if ctx.project_type == "python":
        starter_rows = {
            "Inspect": tr("workspace.inspect_python"),
            "Fix": tr("workspace.fix_python"),
            "Refactor": tr("workspace.refactor_python"),
            "Search": tr("workspace.search_python"),
        }
    elif ctx.project_type == "javascript":
        starter_rows = {
            "Inspect": tr("workspace.inspect_js"),
            "Fix": tr("workspace.fix_js"),
            "Refactor": tr("workspace.refactor_js"),
            "Search": tr("workspace.search_js"),
        }
    else:
        starter_rows = {
            "Inspect": tr("workspace.inspect_generic"),
            "Fix": tr("workspace.fix_generic"),
            "Search": tr("workspace.search_generic"),
            "Build": tr("workspace.build_generic"),
        }

    starter_rows[tr("workspace.top_files")] = top_files
    return workspace_rows, starter_rows


def print_workspace_dashboard(state: Any, mcp_manager: Any = None) -> None:
    """Print the active workspace summary."""
    workspace_rows, starter_rows = format_workspace_snapshot(
        state.context,
        state.workspace,
    )
    workspace_rows[tr("workspace.model")] = f"{state.model_type}/{state.model_config.get('model_name', 'unknown')}"
    message_count = state.session.get_message_count()
    session_prefix = state.session.session_id[:6]
    if state.restored_session and message_count:
        session_status = tr("common.restored_messages", count=message_count)
    elif message_count:
        session_status = tr("common.messages", count=message_count)
    else:
        session_status = tr("common.ready")
    workspace_rows[tr("workspace.session")] = f"{session_prefix} - {session_status}"
    workspace_rows[tr("workspace.streaming")] = on_off(state.stream_output)
    workspace_rows["Mode"] = f"{state.agent_mode} ({agent_mode_label(state.agent_mode)})"
    workspace_rows[tr("workspace.top_files")] = starter_rows.get(tr("workspace.top_files"), tr("workspace.no_files"))

    next_step = starter_rows.get("Fix") or starter_rows.get("Build") or starter_rows.get("Search")
    footer = tr(
        "workspace.footer",
        inspect=starter_rows.get("Inspect"),
        next_step=next_step,
    )
    if mcp_manager and mcp_manager.get_server_count():
        footer += f" | MCP {mcp_manager.get_server_count()}"

    print_summary_card(
        tr("workspace.title"),
        workspace_rows,
        subtitle=tr("workspace.subtitle"),
        footer=footer,
    )
    console.print()


def print_custom_commands_dashboard(workspace: Path) -> None:
    """Show Claude-compatible custom command files."""
    commands = list_custom_commands(workspace)

    if not commands:
        print_info(tr("commands.none1"))
        print_info(tr("commands.none2"))
        return

    rows = {}
    for command in commands[:10]:
        label = command.primary_invocation
        if command.qualified_invocation:
            label = f"{label} ({command.qualified_invocation})"
        rows[label] = command.description or command.source_label

    print_summary_card(
        tr("commands.title"),
        rows,
        subtitle=tr("commands.subtitle"),
        footer=tr("commands.footer"),
    )
    console.print()


def collect_local_path_rows(workspace: Path) -> Dict[str, str]:
    """Collect local user and workspace state paths."""
    resolved_workspace = Path(workspace).expanduser().resolve()
    api_manager = APIConfigManager()
    runtime_paths = workspace_state_paths(resolved_workspace)

    return {
        tr("paths.user_preferences"): str(UserConfig.default_path()),
        tr("paths.model_profiles"): str(api_manager.configs_file),
        tr("paths.workspace_session_index"): str(runtime_paths["index"]),
        tr("paths.workspace_session"): str(runtime_paths["sessions_dir"]),
        tr("paths.project_commands"): str(resolved_workspace / ".claude" / "commands"),
        tr("paths.project_mcp"): str(resolved_workspace / ".mcp.json"),
    }


def print_local_paths_dashboard(workspace: Path) -> None:
    """Show local configuration and state paths."""
    print_summary_card(
        tr("paths.title"),
        collect_local_path_rows(workspace),
        subtitle=tr("paths.subtitle"),
        footer=tr("paths.footer"),
    )
    console.print()


__all__ = [
    "CustomCommandsCommandHandler",
    "PathsCommandHandler",
    "WorkspaceCommandHandler",
    "collect_local_path_rows",
    "format_workspace_snapshot",
    "print_custom_commands_dashboard",
    "print_local_paths_dashboard",
    "print_workspace_dashboard",
]
