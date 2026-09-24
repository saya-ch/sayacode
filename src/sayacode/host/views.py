"""把原生图快照和任务元数据投影为浏览器可读的公开视图。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..agent.events import _message_text
from ..tasks.records import TaskRecord


def session_view(row: Mapping[str, Any], workspace_id: str) -> dict[str, Any]:
    return {
        "id": str(row["thread_id"]),
        "workspace_id": workspace_id,
        "title": str(row.get("title") or "新会话"),
        "status": str(row.get("status") or "idle"),
        "updated_at": row.get("updated_at"),
    }


def task_view(record: TaskRecord, workspace_id: str) -> dict[str, Any]:
    """白名单投影，不能把模型密钥或完整私有任务快照送入页面。"""
    return {
        "id": record.task_id,
        "thread_id": record.thread_id,
        "parent_thread_id": record.parent_thread_id,
        "title": record.title,
        "role": record.role,
        "status": record.status,
        "last_outcome": record.last_outcome,
        "workspace_id": workspace_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "result": record.result,
        "error": record.error,
        "stopped_reason": record.stopped_reason,
        "unconfirmed_effects": record.unconfirmed_effects,
        "recovery_note": record.recovery_note,
        "delivery_state": record.delivery_state,
        "worktree_enabled": record.worktree_enabled,
    }


def message_view(message: Any, index: int) -> dict[str, Any]:
    extras = getattr(message, "additional_kwargs", {})
    role = getattr(message, "type", type(message).__name__)
    if isinstance(extras, dict) and extras.get("sayacode_source") == "agent_inbox":
        role = "agent_inbox"
    return {
        "id": str(getattr(message, "id", None) or f"message-{index}"),
        "role": str(role),
        "text": _message_text(message),
    }


def todo_view(todo: Any, index: int) -> dict[str, str]:
    if isinstance(todo, Mapping):
        return {
            "id": str(todo.get("id") or f"todo-{index}"),
            "content": str(todo.get("content") or todo.get("task") or ""),
            "status": str(todo.get("status") or "pending"),
        }
    return {"id": f"todo-{index}", "content": str(todo), "status": "pending"}


__all__ = ["message_view", "session_view", "task_view", "todo_view"]
