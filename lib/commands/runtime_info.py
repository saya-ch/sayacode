"""Runtime information and maintenance commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import (
    SayacodeColors,
    confirm_action,
    console,
    print_error,
    print_status,
    print_status_info,
    print_success,
    print_summary_card,
)
from .base import CommandContext, CommandHandler


@dataclass
class StatusCommandHandler(CommandHandler):
    """Show runtime status and token budget information."""

    name: str = "status"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        state = runtime.app_state
        agent = runtime.agent
        if state is None or agent is None:
            print_error(tr("runtime.state_unavailable"))
            return True

        stats = agent.get_stats()
        mcp_manager = runtime.mcp
        print_status_info(
            workspace=str(state.workspace),
            model=f"{state.model_type} ({state.model_config.get('model_name', 'unknown')})",
            mcp_servers=mcp_manager.get_server_count() if mcp_manager else 0,
            stream_output=state.stream_output,
        )
        token_rows = {
            tr("runtime.session_messages"): str(stats.get("session_messages", 0)),
            tr("runtime.memory_interactions"): str(stats.get("memory_interactions", 0)),
            tr("runtime.indexed_files"): str(stats.get("context_files", 0)),
            tr("runtime.modified_files"): str(stats.get("modified_files", 0)),
        }
        if "last_total_tokens" in stats:
            token_rows[tr("runtime.last_tokens")] = (
                f"{stats.get('last_prompt_tokens', 0)} + {stats.get('last_completion_tokens', 0)} = "
                f"{stats.get('last_total_tokens', 0)}"
            )
        if "session_total_tokens" in stats:
            token_rows[tr("runtime.session_tokens")] = (
                f"{stats.get('session_prompt_tokens', 0)} + {stats.get('session_completion_tokens', 0)} = "
                f"{stats.get('session_total_tokens', 0)}"
            )

        compact_info = agent.session.get_compact_info()
        token_rows[tr("runtime.context_usage")] = _format_context_usage_ratio(compact_info)
        if compact_info["compact_count"] > 0:
            token_rows[tr("runtime.compacts")] = str(compact_info["compact_count"])

        print_summary_card(
            tr("runtime.title"),
            token_rows,
            subtitle=tr("runtime.subtitle"),
        )
        console.print()
        return True


@dataclass
class StatsCommandHandler(CommandHandler):
    """Show raw runtime stats."""

    name: str = "stats"
    aliases: tuple[str, ...] = ("stat",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        agent = runtime.agent
        if agent is None:
            print_error(tr("runtime.agent_unavailable"))
            return True

        console.print()
        console.print(f"[{SayacodeColors.PRIMARY}]{tr('stats.title')}[/]")
        for key, value in agent.get_stats().items():
            console.print(f"  {key}: {value}")
        console.print()
        return True


@dataclass
class AnalyzeCommandHandler(CommandHandler):
    """Analyze the active project."""

    name: str = "analyze"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        agent = runtime.agent
        if agent is None:
            print_error(tr("runtime.agent_unavailable"))
            return True

        print_status(tr("analyze.start"))
        result = agent.analyze_project()
        print_success(tr("analyze.done"))
        console.print(result)
        console.print()
        return True


@dataclass
class ResetCommandHandler(CommandHandler):
    """Reset the active conversation runtime."""

    name: str = "reset"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        agent = runtime.agent
        if agent is not None and confirm_action(tr("reset.confirm")):
            agent.reset()
            print_success(tr("reset.done"))
        return True


@dataclass
class GitCommandHandler(CommandHandler):
    """Run quick Git inspection commands using runtime-bound tools."""

    name: str = "git"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        console.print()
        console.print(f"[{SayacodeColors.PRIMARY}]{tr('git.title')}[/]")

        git_options = [
            ("1", tr("git.status"), _tool_by_name(runtime, "git_status"), {}),
            ("2", tr("git.log"), _tool_by_name(runtime, "git_log"), {"n": 10}),
            ("3", tr("git.branch"), _tool_by_name(runtime, "git_branch"), {}),
            ("0", tr("git.back"), None, {}),
        ]

        for opt, desc, _, _ in git_options:
            console.print(f"  [{opt}] {desc}")

        choice = console.input("\n  > ").strip()

        for opt, _, tool, arguments in git_options:
            if choice == opt and tool is not None:
                console.print(tool.invoke(arguments))
                break

        return True


def _tool_by_name(runtime: RuntimeContext, name: str) -> Optional[Any]:
    for tool in list(runtime.tools or getattr(runtime.agent, "tools", []) or []):
        if getattr(tool, "name", None) == name:
            return tool
    return None


def _format_context_usage_ratio(compact_info: dict) -> str:
    if not compact_info.get("context_limit_known"):
        return tr("common.not_set")
    return f"{compact_info['usage_ratio']:.0%}"


__all__ = [
    "AnalyzeCommandHandler",
    "GitCommandHandler",
    "ResetCommandHandler",
    "StatsCommandHandler",
    "StatusCommandHandler",
]
