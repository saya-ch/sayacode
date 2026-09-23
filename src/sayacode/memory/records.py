"""长期记忆的产品记录；对话正文仍由 LangGraph 检查点保存。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

MemoryKind = Literal["user", "project"]
MemoryState = Literal["active", "candidate", "needs_verification", "replaced"]
SourceKind = Literal["user", "tool", "assistant", "task"]
ChangeAction = Literal["insert", "update", "replace", "retire"]


@dataclass(frozen=True)
class MemoryScope:
    """把用户或项目身份固定到一个 Store 命名空间。"""

    kind: MemoryKind
    identity: str

    def __post_init__(self) -> None:
        if self.kind not in ("user", "project") or not self.identity:
            raise ValueError("记忆作用域需要有效的类型和身份")

    @property
    def namespace(self) -> tuple[str, ...]:
        return ("sayacode", "memory", self.kind, self.identity)


@dataclass(frozen=True)
class MemorySource:
    """原始证据的引用；不复制聊天或工具输出。"""

    ref: str
    kind: SourceKind
    order: str
    thread_id: str = ""
    checkpoint_id: str = ""
    project_id: str = ""
    user_message_id: str = ""
    evidence_refs: tuple[str, ...] = ()
    profile_name: str = ""
    model_identity_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.ref or not self.order or self.kind not in (
            "user",
            "tool",
            "assistant",
            "task",
        ):
            raise ValueError("记忆来源需要引用、类型和原始顺序")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MemorySource:
        return cls(
            ref=str(value["ref"]),
            kind=value["kind"],
            order=str(value["order"]),
            thread_id=str(value.get("thread_id", "")),
            checkpoint_id=str(value.get("checkpoint_id", "")),
            project_id=str(value.get("project_id", "")),
            user_message_id=str(value.get("user_message_id", "")),
            evidence_refs=tuple(str(ref) for ref in value.get("evidence_refs", ())),
            profile_name=str(value.get("profile_name", "")),
            model_identity_sha256=str(value.get("model_identity_sha256", "")),
        )


@dataclass(frozen=True)
class MemoryRecord:
    """一条可追溯且可失效的学习记忆。"""

    id: str
    version: int
    subject: str
    text: str
    scope: MemoryScope
    state: MemoryState
    sources: tuple[MemorySource, ...]
    applicability: Mapping[str, str]
    created_at: str
    updated_at: str
    source_order: str
    confirmed_at: str | None = None
    review_after: str | None = None
    expires_at: str | None = None
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], scope: MemoryScope) -> MemoryRecord:
        stored_scope = value.get("scope", {})
        if stored_scope != {"kind": scope.kind, "identity": scope.identity}:
            raise ValueError("记忆记录的作用域与 Store 命名空间不符")
        state = value["state"]
        if state not in ("active", "candidate", "needs_verification", "replaced"):
            raise ValueError("记忆 Store 中的记录状态无效")
        return cls(
            id=str(value["id"]),
            version=int(value["version"]),
            subject=str(value["subject"]),
            text=str(value["text"]),
            scope=scope,
            state=state,
            sources=tuple(MemorySource.from_dict(item) for item in value["sources"]),
            applicability=dict(value.get("applicability", {})),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            source_order=str(value["source_order"]),
            confirmed_at=value.get("confirmed_at"),
            review_after=value.get("review_after"),
            expires_at=value.get("expires_at"),
            pinned=bool(value.get("pinned", False)),
        )


@dataclass(frozen=True)
class MemoryJob:
    """等待整理的来源和短租约；模型执行时不持有文件锁。"""

    id: str
    source: MemorySource
    status: Literal["pending", "claimed", "completed"] = "pending"
    lease_token: str = ""
    lease_owner: str = ""
    lease_until: str | None = None
    attempts: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MemoryJob:
        status = value.get("status", "pending")
        if status not in ("pending", "claimed", "completed"):
            raise ValueError("记忆 Store 中的工作状态无效")
        return cls(
            id=str(value["id"]),
            source=MemorySource.from_dict(value["source"]),
            status=status,
            lease_token=str(value.get("lease_token", "")),
            lease_owner=str(value.get("lease_owner", "")),
            lease_until=value.get("lease_until"),
            attempts=int(value.get("attempts", 0)),
            last_error=str(value.get("last_error", "")),
        )


@dataclass(frozen=True)
class MemoryChange:
    """提取器提出的内容修订；来源身份只能由待整理工作提供。"""

    action: ChangeAction
    subject: str = ""
    text: str = ""
    record_id: str = ""
    state: MemoryState = "active"
    candidate_key: str = ""
    applicability: Mapping[str, str] = field(default_factory=dict)
    reaffirmed: bool = False
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemorySnapshot:
    """用于提取和提交校验的只读作用域视图。"""

    scope: MemoryScope
    revision: int
    control_epoch: int
    records: Mapping[str, MemoryRecord]
    pending: Mapping[str, MemoryJob]
    processed: Mapping[str, Mapping[str, Any]]
    tombstones: Mapping[str, Mapping[str, Any]]


class MemoryConflictError(RuntimeError):
    """来源或版本已过时，不能覆盖当前记忆。"""


__all__ = [
    "MemoryChange",
    "MemoryConflictError",
    "MemoryJob",
    "MemoryRecord",
    "MemoryScope",
    "MemorySnapshot",
    "MemorySource",
    "MemoryState",
]
