"""Workspace-scoped session persistence for SAYACODE."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import json

from ..core.memory import MemoryManager
from ..core.private_io import ensure_private_dir, write_private_json, write_private_text
from ..core.session import SessionManager
from ..core.modes import normalize_agent_mode
from ..core.paths import StateStore
from ..prompts import normalize_prompt_style


def workspace_state_dir(workspace: Path) -> Path:
    """Return the stable state directory for one workspace."""
    return StateStore().workspace_state_dir(workspace)


def workspace_state_paths(workspace: Path) -> Dict[str, Path]:
    """Return session and memory paths for one workspace."""
    return StateStore().workspace_state_paths(workspace)


def workspace_session_paths(workspace: Path, session_id: str) -> Dict[str, Path]:
    """Return persistence paths for one concrete workspace session."""
    return StateStore().workspace_session_paths(workspace, session_id)


def new_workspace_session_index(workspace: Path) -> Dict[str, Any]:
    """Create an empty workspace session index."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": 1,
        "workspace": str(Path(workspace).expanduser().resolve()),
        "active_session_id": None,
        "created_at": now,
        "last_updated": now,
        "sessions": [],
    }


def load_workspace_session_index(workspace: Path) -> Dict[str, Any]:
    """Load the workspace session index, returning an empty index on corruption."""
    paths = workspace_state_paths(workspace)
    index = new_workspace_session_index(workspace)

    if not paths["index"].exists():
        return index

    try:
        loaded = json.loads(paths["index"].read_text(encoding="utf-8"))
    except Exception:
        return index

    if isinstance(loaded, dict):
        index.update({key: value for key, value in loaded.items() if key in index})
        if not isinstance(index.get("sessions"), list):
            index["sessions"] = []
        if not index.get("workspace"):
            index["workspace"] = str(Path(workspace).expanduser().resolve())

    return index


def write_workspace_session_index(workspace: Path, index: Dict[str, Any]) -> None:
    """Save the workspace session index."""
    paths = workspace_state_paths(workspace)
    index["last_updated"] = datetime.now(timezone.utc).isoformat()
    write_private_json(paths["index"], index)


def derive_session_title(session: SessionManager, fallback: Optional[str] = None) -> str:
    """Derive a readable session title from the first user message."""
    if fallback:
        return fallback.strip()[:80]

    for message in session.messages:
        if message.role == "user":
            title = " ".join(message.content.strip().split())
            if title:
                return title[:80]

    return f"Session {session.session_id}"


def session_index_entry(
    workspace: Path,
    session: SessionManager,
    memory: MemoryManager,
    title: Optional[str] = None,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one workspace session index entry."""
    existing = existing or {}
    session_paths = workspace_session_paths(workspace, session.session_id)
    state_dir = workspace_state_paths(workspace)["dir"]

    def _relative(path: Path) -> str:
        try:
            return str(path.relative_to(state_dir))
        except ValueError:
            return str(path)

    return {
        "session_id": session.session_id,
        "title": derive_session_title(session, title or existing.get("title")),
        "created_at": session.created_at.isoformat(),
        "last_updated": session.last_updated.isoformat(),
        "messages": session.get_message_count(),
        "interactions": len(memory),
        "session_path": _relative(session_paths["session"]),
        "memory_path": _relative(session_paths["memory"]),
        "context_path": _relative(session_paths["context"]),
    }


def upsert_workspace_session_index(
    workspace: Path,
    session: SessionManager,
    memory: MemoryManager,
    title: Optional[str] = None,
) -> None:
    """Update the workspace index and mark this session active."""
    index = load_workspace_session_index(workspace)
    existing_entries = [
        entry
        for entry in index.get("sessions", [])
        if isinstance(entry, dict) and entry.get("session_id")
    ]
    existing_by_id = {entry["session_id"]: entry for entry in existing_entries}
    next_entry = session_index_entry(
        workspace,
        session,
        memory,
        title=title,
        existing=existing_by_id.get(session.session_id),
    )

    index["active_session_id"] = session.session_id
    index["sessions"] = [
        entry for entry in existing_entries
        if entry.get("session_id") != session.session_id
    ] + [next_entry]
    write_workspace_session_index(workspace, index)


def resolve_workspace_session_id(workspace: Path, requested: Optional[str] = None) -> Optional[str]:
    """Resolve a workspace session ID, including unique prefixes."""
    index = load_workspace_session_index(workspace)
    session_ids = [
        entry.get("session_id")
        for entry in index.get("sessions", [])
        if isinstance(entry, dict) and entry.get("session_id")
    ]

    if requested:
        if requested in session_ids:
            return requested
        matches = [session_id for session_id in session_ids if session_id.startswith(requested)]
        if len(matches) == 1:
            return matches[0]
        try:
            session_paths = workspace_session_paths(workspace, requested)
        except ValueError:
            return None
        if session_paths["session"].exists() or session_paths["memory"].exists():
            return requested
        return None

    active_session_id = index.get("active_session_id")
    if active_session_id:
        return str(active_session_id)

    return session_ids[-1] if session_ids else None


def load_session_memory_pair(
    workspace: Path,
    session_id: str,
    max_history: int = 50,
) -> tuple[SessionManager, MemoryManager, bool]:
    """Load one SessionManager and MemoryManager pair."""
    paths = workspace_session_paths(workspace, session_id)
    restored = False

    session = SessionManager.load(str(paths["session"]))
    if session:
        restored = True
    else:
        session = create_session(workspace, max_messages=100, session_id=session_id)
        memory = MemoryManager(max_history=max_history, session_id=session.session_id)
        return session, memory, False

    memory = MemoryManager(max_history=max_history, session_id=session.session_id)
    if paths["memory"].exists():
        try:
            if memory.load_from_json(paths["memory"].read_text(encoding="utf-8")):
                restored = True
        except Exception:
            pass

    if not memory.interactions:
        memory.session_id = session.session_id
    elif memory.session_id != session.session_id:
        session.session_id = memory.session_id

    return session, memory, restored


def list_workspace_sessions(workspace: Path) -> List[Dict[str, Any]]:
    """List saved sessions for one workspace."""
    index = load_workspace_session_index(workspace)
    entries = [
        entry for entry in index.get("sessions", [])
        if isinstance(entry, dict) and entry.get("session_id")
    ]

    if entries:
        return sorted(entries, key=lambda entry: entry.get("last_updated", ""), reverse=True)

    return []


def session_archive_dir(workspace: Path) -> Optional[str]:
    """Return the archive directory for compacted session history."""
    archive_dir = workspace_state_dir(workspace) / "session_archive"
    return str(archive_dir)


def create_session(workspace: Optional[Path], **kwargs: Any) -> SessionManager:
    """Create a SessionManager with the workspace archive directory attached."""
    if workspace:
        kwargs.setdefault("archive_dir", session_archive_dir(workspace))
    return SessionManager(**kwargs)


def load_runtime_managers(
    workspace: Path,
    max_history: int = 50,
    requested_session_id: Optional[str] = None,
    create_new: bool = False,
) -> tuple[SessionManager, MemoryManager, bool]:
    """Restore the active workspace session or create a new one."""
    if create_new:
        session = create_session(workspace, max_messages=100)
        memory = MemoryManager(max_history=max_history, session_id=session.session_id)
        return session, memory, False

    selected_session_id = resolve_workspace_session_id(workspace, requested_session_id)
    if selected_session_id:
        return load_session_memory_pair(workspace, selected_session_id, max_history=max_history)

    session = create_session(workspace, max_messages=100)
    memory = MemoryManager(max_history=max_history, session_id=session.session_id)
    return session, memory, False


def save_runtime_state(state: Any, session_title: Optional[str] = None) -> None:
    """Persist the active workspace session and memory."""
    state.memory.session_id = state.session.session_id
    session_paths = workspace_session_paths(state.workspace, state.session.session_id)
    ensure_private_dir(session_paths["dir"])
    state.session.save(str(session_paths["session"]))
    write_private_text(session_paths["memory"], state.memory.export_to_json() + "\n", encoding="utf-8")
    if state.context is not None:
        state.context.save_context(str(session_paths["context"]))

    upsert_workspace_session_index(
        state.workspace,
        state.session,
        state.memory,
        title=session_title,
    )


def persist_local_state(state: Any, user_config: Optional[Any] = None) -> None:
    """Persist runtime state and user preferences."""
    save_runtime_state(state)

    if user_config is not None:
        user_config.workspace = str(state.workspace)
        user_config.active_profile = state.active_profile
        user_config.stream_output = state.stream_output
        user_config.confirm_dangerous = state.confirm_dangerous
        user_config.prompt_style = normalize_prompt_style(state.prompt_style)
        user_config.agent_mode = normalize_agent_mode(state.agent_mode) or "build"
        user_config.last_used_at = datetime.now(timezone.utc).isoformat()
        user_config.save()


def sync_session_model_runtime(session: SessionManager, model: Any) -> None:
    """Synchronize session budgeting and compaction callbacks to a model."""
    if hasattr(model, "context_window") and model.context_window > 0:
        session.set_context_limit(model.context_window)
    if hasattr(model, "chat"):
        session.set_compact_fn(model.chat)


def attach_session_to_runtime(
    agent: Any,
    state: Any,
    session: SessionManager,
    memory: MemoryManager,
    restored: bool,
) -> None:
    """Synchronize a newly loaded session pair to AppState, Agent, and RuntimeContext."""
    state.session = session
    state.memory = memory
    state.restored_session = restored
    state.update()

    if hasattr(agent, "session"):
        agent.session = session
        if hasattr(agent, "model"):
            sync_session_model_runtime(agent.session, agent.model)
    if hasattr(agent, "memory"):
        agent.memory = memory
    if hasattr(agent, "conversation_manager"):
        agent.conversation_manager.session = session
        agent.conversation_manager.memory = memory

    runtime_context = getattr(state, "runtime_context", None)
    if runtime_context is not None:
        runtime_context.sync_from_app_state(state)
        runtime_context.attach_agent(agent)


__all__ = [
    "attach_session_to_runtime",
    "create_session",
    "derive_session_title",
    "list_workspace_sessions",
    "load_runtime_managers",
    "load_session_memory_pair",
    "persist_local_state",
    "resolve_workspace_session_id",
    "save_runtime_state",
    "session_index_entry",
    "sync_session_model_runtime",
    "workspace_session_paths",
    "workspace_state_dir",
    "workspace_state_paths",
]
