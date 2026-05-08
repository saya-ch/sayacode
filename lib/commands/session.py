"""Session slash command."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.memory import MemoryManager
from ..i18n import tr
from ..runtime import RuntimeContext
from ..runtime.session_store import (
    attach_session_to_runtime,
    create_session,
    derive_session_title,
    list_workspace_sessions,
    load_session_memory_pair,
    resolve_workspace_session_id,
    save_runtime_state,
    session_index_entry,
    workspace_session_paths,
)
from ..theme import console, print_error, print_info, print_success, print_summary_card
from .base import CommandContext, CommandHandler


@dataclass
class SessionCommandHandler(CommandHandler):
    """Manage workspace sessions."""

    name: str = "sessions"
    aliases: tuple[str, ...] = ("session", "new")

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        state = runtime.app_state
        agent = runtime.agent
        if state is None or agent is None:
            print_error(tr("runtime.state_unavailable"))
            return True

        raw_parts = command.raw.strip().split(maxsplit=2)
        action = raw_parts[1].lower() if len(raw_parts) > 1 and command.name != "new" else "list"
        if command.name == "new":
            action = "new"

        if action in {"list", "ls"}:
            print_session_dashboard(state)
            return True

        if action in {"current", "show"}:
            print_current_session_dashboard(state)
            return True

        if action in {"help", "-h", "--help"}:
            print_summary_card(
                tr("session.commands_title"),
                {
                    "/session list": tr("session.commands.list"),
                    "/session current": tr("session.commands.current"),
                    "/session new [title]": tr("session.commands.new"),
                    "/session use <id>": tr("session.commands.use"),
                    "/session rename <title>": tr("session.commands.rename"),
                },
                footer=tr("session.commands_footer"),
            )
            console.print()
            return True

        if action in {"new", "create"}:
            title = raw_parts[2].strip() if len(raw_parts) > 2 else None
            save_runtime_state(state)

            session = create_session(
                state.workspace,
                max_messages=state.session.max_messages,
                enable_summary=state.session.enable_summary,
            )
            memory = MemoryManager(
                max_history=state.memory.max_history,
                max_file_records=state.memory.max_file_records,
                session_id=session.session_id,
            )
            attach_session_to_runtime(agent, state, session, memory, restored=False)
            save_runtime_state(state, session_title=title)
            print_success(tr("session.created", id=session.session_id))
            print_current_session_dashboard(state)
            return True

        if action in {"use", "open", "switch"}:
            if len(raw_parts) < 3 or not raw_parts[2].strip():
                print_error(tr("session.usage_use"))
                return True

            requested_id = raw_parts[2].strip().split()[0]
            session_id = resolve_workspace_session_id(state.workspace, requested_id)
            if not session_id:
                print_error(tr("session.not_found", id=requested_id))
                return True

            save_runtime_state(state)
            session, memory, restored = load_session_memory_pair(
                state.workspace,
                session_id,
                max_history=state.memory.max_history,
            )
            attach_session_to_runtime(agent, state, session, memory, restored=restored)
            save_runtime_state(state)
            print_success(tr("session.switched", id=session.session_id))
            print_current_session_dashboard(state)
            return True

        if action in {"rename", "title"}:
            if len(raw_parts) < 3 or not raw_parts[2].strip():
                print_error(tr("session.usage_rename"))
                return True

            title = raw_parts[2].strip()
            save_runtime_state(state, session_title=title)
            print_success(tr("session.renamed", title=title))
            print_current_session_dashboard(state)
            return True

        print_error(tr("session.unknown_command"))
        return True


def print_session_dashboard(state: object) -> None:
    """Show the workspace session list."""
    entries = list_workspace_sessions(state.workspace)
    current_id = state.session.session_id
    entry_ids = {entry.get("session_id") for entry in entries}

    if current_id not in entry_ids:
        entries.insert(0, session_index_entry(state.workspace, state.session, state.memory))

    if not entries:
        print_info(tr("session.none"))
        return

    rows = {}
    for entry in entries[:10]:
        session_id = str(entry.get("session_id", ""))
        marker = "*" if session_id == current_id else " "
        title = entry.get("title") or f"Session {session_id}"
        updated = str(entry.get("last_updated") or "")[:19]
        messages = entry.get("messages", 0)
        interactions = entry.get("interactions", 0)
        rows[f"{marker} {session_id}"] = tr(
            "session.list_item",
            title=title,
            messages=messages,
            interactions=interactions,
            updated=updated or tr("common.not_set"),
        )

    print_summary_card(
        tr("session.manager_title"),
        rows,
        subtitle=tr("session.manager_subtitle"),
        footer=tr("session.manager_footer"),
    )
    console.print()


def print_current_session_dashboard(state: object) -> None:
    """Show details for the current session."""
    print_summary_card(
        tr("session.current_title"),
        {
            tr("session.id"): state.session.session_id,
            tr("session.title_label"): derive_session_title(state.session),
            tr("runtime.session_messages"): str(state.session.get_message_count()),
            tr("runtime.memory_interactions"): str(len(state.memory)),
            tr("session.created_at"): state.session.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            tr("session.updated_at"): state.session.last_updated.strftime("%Y-%m-%d %H:%M:%S"),
        },
        subtitle=str(workspace_session_paths(state.workspace, state.session.session_id)["dir"]),
        footer=tr("session.current_footer"),
    )
    console.print()


__all__ = ["SessionCommandHandler", "print_current_session_dashboard", "print_session_dashboard"]
