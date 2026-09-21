"""后台任务状态与公开结果，只存元数据不跑逻辑。

任务一生只沿一条状态链走，中间态可恢复，终态会涨序号。
中间态是待定运行中和正在停止，终态是完成失败暂停停止和中断。
终态里只有完成失败暂停停止会通知父线程，中断只做本机标记。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


class TaskError(RuntimeError):
    """任务或工作树操作未能安全完成，调用方应直接报错不重试。"""


class TaskPaused(RuntimeError):
    """工作者遇到中断，需要终端输入才能继续，恢复前不要清现场。"""


def task_title(prompt: str, title: str | None = None) -> str:
    """生成可放入终端列表的短任务标题。"""
    chosen = " ".join(str(title or prompt).split())
    return chosen[:48] + ("…" if len(chosen) > 48 else "")


@dataclass(slots=True)
class TaskRecord:
    """一条后台任务的完整档案，是状态机流转的载体。

    做什么，记住谁在跑、在哪跑、跑到哪一步、结果在哪。
    参数与返回，字段即全量状态，序号每进一次终态加一。
    调用约束，终端展示用展示视图，落盘用存储视图，不要混用。
    坑点是快照里可能带密钥，展示视图会脱敏，存储视图保留原文。"""

    task_id: str
    thread_id: str
    parent_thread_id: str | None
    role: str
    prompt: str
    workspace: str
    worktree_enabled: bool
    title: str = ""
    pending_input: str | None = None
    status: str = "pending"
    last_outcome: str | None = None
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
    context_snapshot: dict[str, Any] | None = None
    trust_level: str = "ask"
    unconfirmed_effects: bool = False
    recovery_note: str | None = None
    turn_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        """做什么，返回展示和审计可用的任务元数据视图。

        参数与返回，无入参，返回全字段字典，密钥会被遮蔽。
        调用约束，只给界面和审计用，不要拿它再存回去。"""
        data = asdict(self)
        snapshot = data.get("profile_snapshot")
        if isinstance(snapshot, dict) and snapshot.get("api_key"):
            snapshot["api_key"] = "***"
        return data

    def to_store_dict(self) -> dict[str, Any]:
        """做什么，返回重建任务模型所需的私有存储载荷。

        参数与返回，无入参，返回全字段字典，密钥原文保留。
        调用约束，只写存储用，不要直接展示给模型或终端。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        """做什么，从存储字典重建任务档案。

        参数与返回，入参是存储字典，返回任务档案对象。
        调用约束，多余字段会被丢弃，缺字段会直接报错。
        坑点是旧版本多存的字段读不回来，以当前类定义为准。"""
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})
