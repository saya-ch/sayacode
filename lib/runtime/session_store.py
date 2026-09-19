"""SAYACODE 的 workspace 作用域 session 持久化。

负责 session 索引、加载与落盘，核心函数为 save_runtime_state、
persist_local_state 与 load_runtime_managers，供启动与交互循环调用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import logging

from ..core.private_io import ensure_private_dir, write_private_json
from lib.core.session_messages import (
    SessionDerivedMemoryView,
    SessionManager,
    load_legacy_memory_json,
)
from ..core.modes import normalize_agent_mode
from ..core.paths import StateStore
from ..prompts import normalize_prompt_style


logger = logging.getLogger(__name__)


def workspace_state_dir(workspace: Path) -> Path:
    """返回单个 workspace 的稳定 state 目录。"""
    return StateStore().workspace_state_dir(workspace)


def workspace_state_paths(workspace: Path) -> Dict[str, Path]:
    """返回单个 workspace 的 session 与 memory 路径。"""
    return StateStore().workspace_state_paths(workspace)


def workspace_session_paths(workspace: Path, session_id: str) -> Dict[str, Path]:
    """返回单个具体 workspace session 的持久化路径。"""
    return StateStore().workspace_session_paths(workspace, session_id)


def new_workspace_session_index(workspace: Path) -> Dict[str, Any]:
    """创建空的 workspace session 索引。"""
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
    """加载 workspace session 索引，损坏时返回空索引。"""
    paths = workspace_state_paths(workspace)
    index = new_workspace_session_index(workspace)

    if not paths["index"].exists():
        return index

    try:
        loaded = json.loads(paths["index"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return index

    if isinstance(loaded, dict):
        index.update({key: value for key, value in loaded.items() if key in index})
        if not isinstance(index.get("sessions"), list):
            index["sessions"] = []
        if not index.get("workspace"):
            index["workspace"] = str(Path(workspace).expanduser().resolve())

    return index


def write_workspace_session_index(workspace: Path, index: Dict[str, Any]) -> None:
    """保存 workspace session 索引。"""
    paths = workspace_state_paths(workspace)
    index["last_updated"] = datetime.now(timezone.utc).isoformat()
    write_private_json(paths["index"], index)


def derive_session_title(session: SessionManager, fallback: Optional[str] = None) -> str:
    """从第一条 user 消息推导可读的 session 标题。"""
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
    memory: Any = None,
    title: Optional[str] = None,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建一条 workspace session 索引记录。"""
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
        "interactions": len(memory) if memory is not None else len(session.derived_interaction_pairs()),
        "session_path": _relative(session_paths["session"]),
        "memory_path": _relative(session_paths["memory"]),
        "context_path": _relative(session_paths["context"]),
    }


def upsert_workspace_session_index(
    workspace: Path,
    session: SessionManager,
    memory: Any = None,
    title: Optional[str] = None,
) -> None:
    """更新 workspace 索引并将此 session 标记为 active。"""
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
    """解析 workspace session ID，包括唯一前缀匹配。"""
    index = load_workspace_session_index(workspace)
    session_ids: list[str] = []
    for entry in index.get("sessions", []):
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("session_id")
        if isinstance(session_id, str) and session_id:
            session_ids.append(session_id)

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
) -> tuple[SessionManager, Any, bool]:
    """加载会话，历史唯一真相源为会话文件，旧记忆文件仅兼容读。

    返回三元组形状不变，第二位为会话派生只读视图。
    旧记忆文件存在且会话文件缺失时，把旧交互导入会话后返回。
    会话文件存在时以会话为准，不再回写旧记忆格式。
    """
    paths = workspace_session_paths(workspace, session_id)
    restored = False

    session = SessionManager.load(str(paths["session"]))
    if session:
        restored = True
        return session, SessionDerivedMemoryView(session), True

    session = create_session(workspace, max_messages=100, session_id=session_id)
    legacy_items: list = []
    if paths["memory"].exists():
        try:
            legacy_items = load_legacy_memory_json(paths["memory"].read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("记忆恢复失败: %s", exc)
            legacy_items = []
    if legacy_items:
        try:
            for msg in SessionManager.messages_from_interaction_dicts(legacy_items):
                session.messages.append(msg)
            session._rebuild_token_count()
            restored = True
        except Exception as exc:
            logger.warning("记忆恢复失败: %s", exc)
    return session, SessionDerivedMemoryView(session), restored


def list_workspace_sessions(workspace: Path) -> List[Dict[str, Any]]:
    """列出单个 workspace 已保存的 sessions。"""
    index = load_workspace_session_index(workspace)
    entries = [
        entry for entry in index.get("sessions", [])
        if isinstance(entry, dict) and entry.get("session_id")
    ]

    if entries:
        return sorted(entries, key=lambda entry: entry.get("last_updated", ""), reverse=True)

    return []


def session_archive_dir(workspace: Path) -> Optional[str]:
    """返回压缩后 session 历史的归档目录。"""
    archive_dir = workspace_state_dir(workspace) / "session_archive"
    return str(archive_dir)


def create_session(workspace: Optional[Path], **kwargs: Any) -> SessionManager:
    """创建附带 workspace 归档目录的 SessionManager。"""
    if workspace:
        kwargs.setdefault("archive_dir", session_archive_dir(workspace))
    return SessionManager(**kwargs)


def load_runtime_managers(
    workspace: Path,
    max_history: int = 50,
    requested_session_id: Optional[str] = None,
    create_new: bool = False,
) -> tuple[SessionManager, Any, bool]:
    """恢复 active workspace session，或创建一个新的（记忆为派生视图）。"""
    if create_new:
        session = create_session(workspace, max_messages=100)
        return session, SessionDerivedMemoryView(session), False

    selected_session_id = resolve_workspace_session_id(workspace, requested_session_id)
    if selected_session_id:
        return load_session_memory_pair(workspace, selected_session_id, max_history=max_history)

    session = create_session(workspace, max_messages=100)
    return session, SessionDerivedMemoryView(session), False


def save_runtime_state(state: Any, session_title: Optional[str] = None) -> None:
    """持久化当前会话，只写会话文件，不再写记忆镜像。

    旧记忆文件如已存在则原样保留，不删用户数据，但不再更新。
    下次加载仅在会话文件缺失时兼容读入。
    """
    session_paths = workspace_session_paths(state.workspace, state.session.session_id)
    ensure_private_dir(session_paths["dir"])
    state.session.save(str(session_paths["session"]))
    if state.context is not None:
        state.context.save_context(str(session_paths["context"]))

    upsert_workspace_session_index(
        state.workspace,
        state.session,
        state.memory,
        title=session_title,
    )


def persist_local_state(state: Any, user_config: Optional[Any] = None) -> None:
    """持久化 runtime 状态与用户偏好。"""
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
    """将 session 预算与压缩 callback 同步到模型。"""
    if hasattr(model, "context_window") and model.context_window > 0:
        session.set_context_limit(model.context_window)
    if hasattr(model, "chat"):
        session.set_compact_fn(model.chat)


def attach_session_to_runtime(
    agent: Any,
    state: Any,
    session: SessionManager,
    memory: Any = None,
    restored: bool = False,
) -> None:
    """将新加载的 session 同步到 AppState、Agent 和 RuntimeContext（记忆为派生视图）。"""
    view = memory if memory is not None else SessionDerivedMemoryView(session)
    state.session = session
    state.memory = view
    state.restored_session = restored
    state.update()

    if hasattr(agent, "session"):
        agent.session = session
        if hasattr(agent, "model"):
            sync_session_model_runtime(agent.session, agent.model)
    if hasattr(agent, "memory"):
        agent.memory = view
    if hasattr(agent, "conversation_manager"):
        agent.conversation_manager.session = session
        agent.conversation_manager.memory = view
    # 会话切换后轮次计数必须归零，否则新会话沿用旧 turn 号导致 store 覆盖。
    try:
        if hasattr(agent, "_turn_count"):
            agent._turn_count = 0
        if hasattr(agent, "_last_extra"):
            agent._last_extra = {}
        if hasattr(agent, "_stream_tokens_seen"):
            agent._stream_tokens_seen = False
    except Exception:
        pass

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
