"""后台任务状态与公开结果。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


class TaskError(RuntimeError):
    """任务或工作树操作未能安全完成。"""


class TaskPaused(RuntimeError):
    """工作者遇到中断。需要终端输入才能继续。"""


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    thread_id: str
    parent_thread_id: str | None
    role: str
    prompt: str
    workspace: str
    worktree_enabled: bool
    pending_input: str | None = None
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    worktree_root: str | None = None
    task_workspace: str | None = None
    branch: str | None = None
    snapshot_commit: str | None = None
    stopped_reason: str | None = None
    error: str | None = None
    result: str | None = None
    delivery_state: str = "none"
    applied_patch_sha256: str | None = None
    profile_name: str | None = None
    profile_snapshot: dict[str, Any] | None = None
    trust_level: str = "ask"
    unconfirmed_effects: bool = False
    recovery_note: str | None = None
    completion_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        """返回展示和审计可用的任务元数据视图。"""
        data = asdict(self)
        snapshot = data.get("profile_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("api_key"):
            snapshot["api_key"] = "***"
        return data

    def to_store_dict(self) -> dict[str, Any]:
        """返回重建任务模型所需的私有存储载荷。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})
