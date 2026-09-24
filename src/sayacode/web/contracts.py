"""本机 Web 层与运行宿主之间的窄接口。

Web 层只消费显式操作和投影，不持有 LangGraph 对象，也不读写 checkpoint。
宿主负责校验工作区、线程和任务之间的归属关系。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """响应只保留声明的字段，避免将私有存储载荷直接送给浏览器。"""

    model_config = ConfigDict(extra="ignore")


class WorkspaceView(ApiModel):
    id: str
    path: str
    name: str
    active_session_id: str | None = None


class SessionView(ApiModel):
    id: str
    workspace_id: str
    title: str
    status: str = "idle"
    updated_at: str | None = None


class MessageView(ApiModel):
    id: str
    role: str
    text: str
    created_at: str | None = None


class TodoView(ApiModel):
    id: str
    content: str
    status: str


class ApprovalActionView(ApiModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class PendingApprovalView(ApiModel):
    checkpoint_id: str
    actions: list[ApprovalActionView]


class ActivityView(ApiModel):
    id: str
    type: str
    at: str | None = None
    summary: str | None = None
    tool_name: str | None = None
    status: str | None = None
    duration_ms: float | None = None
    data: dict[str, Any] | None = None


class ActiveRunView(ApiModel):
    run_id: str
    started_at: str
    status: str


class TaskView(ApiModel):
    id: str
    thread_id: str
    parent_thread_id: str | None = None
    title: str
    role: str
    status: str
    last_outcome: str | None = None
    workspace_id: str
    created_at: str | None = None
    updated_at: str | None = None
    result: str | None = None
    error: str | None = None
    stopped_reason: str | None = None
    unconfirmed_effects: bool = False
    recovery_note: str | None = None
    delivery_state: str | None = None
    worktree_enabled: bool | None = None


class ThreadSnapshot(ApiModel):
    thread_id: str
    workspace_id: str
    title: str
    status: str
    trust_level: str | None = None
    messages: list[MessageView] = Field(default_factory=list)
    todos: list[TodoView] = Field(default_factory=list)
    pending_approval: PendingApprovalView | None = None
    tasks: list[TaskView] = Field(default_factory=list)
    activity: list[ActivityView] = Field(default_factory=list)
    active_run: ActiveRunView | None = None


class StatusView(ApiModel):
    model: str | None = None
    protocol: str | None = None
    trust_level: str | None = None
    workspace_id: str | None = None
    session_id: str | None = None
    running_tasks: int = 0
    running_agents: int = 0


class SettingsView(ApiModel):
    language: str = "auto"
    default_trust: str = "ask"
    active_profile: str | None = None
    memory_enabled: bool = False
    output_limit_bytes: int = 64 * 1024
    task_notice_limit_bytes: int = 64 * 1024
    max_consecutive_wakes: int = 8
    shutdown_grace_seconds: float = 10.0


class RunReceipt(ApiModel):
    run_id: str
    thread_id: str
    status: str


class EventEnvelope(ApiModel):
    seq: int = Field(ge=0)
    instance_id: str
    type: str
    workspace_id: str
    thread_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    at: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class WebHostProtocol(Protocol):
    """Web 传输要求的产品操作；方法不依赖 FastAPI 类型。"""

    async def status(self) -> Mapping[str, Any]: ...
    async def settings(self) -> Mapping[str, Any]: ...
    async def update_settings(self, patch: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def list_workspaces(self) -> Sequence[Mapping[str, Any]]: ...
    async def create_workspace(self, path: str, name: str | None) -> Mapping[str, Any]: ...
    async def rename_workspace(self, workspace_id: str, name: str) -> Mapping[str, Any]: ...
    async def list_sessions(self, workspace_id: str) -> Sequence[Mapping[str, Any]]: ...
    async def create_session(self, workspace_id: str, title: str | None) -> Mapping[str, Any]: ...
    async def rename_thread(self, thread_id: str, title: str) -> Mapping[str, Any]: ...
    async def set_trust(self, thread_id: str, trust_level: str) -> Mapping[str, Any]: ...
    async def thread_snapshot(self, thread_id: str) -> Mapping[str, Any]: ...
    async def list_thread_tasks(self, thread_id: str) -> Sequence[Mapping[str, Any]]: ...
    async def start_run(self, thread_id: str, message: str) -> Mapping[str, Any]: ...
    async def decide_approval(
        self,
        thread_id: str,
        checkpoint_id: str,
        decisions: list[dict[str, str]],
        grants: list[dict[str, Any]],
    ) -> Mapping[str, Any]: ...
    async def list_tasks(self, workspace_id: str | None) -> Sequence[Mapping[str, Any]]: ...
    async def spawn_task(
        self,
        parent_thread_id: str,
        role: str,
        prompt: str,
        title: str | None,
        worktree_enabled: bool | None,
    ) -> Mapping[str, Any]: ...
    async def task_action(
        self, task_id: str, action: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    def subscribe(
        self, workspace_id: str | None, after: int | None, instance_id: str | None
    ) -> AsyncIterator[Mapping[str, Any]]: ...

    async def list_checkpoints(self, thread_id: str) -> Sequence[Mapping[str, Any]]: ...
    async def compact_thread(self, thread_id: str, focus: str | None) -> Mapping[str, Any]: ...
    async def rewind_thread(self, thread_id: str, checkpoint_id: str) -> Mapping[str, Any]: ...
    async def trace_thread(
        self, thread_id: str, run_id: str | None
    ) -> Sequence[Mapping[str, Any]]: ...
    async def list_thread_tools(self, thread_id: str) -> Sequence[Mapping[str, Any]]: ...
    async def clear_thread_grants(self, thread_id: str) -> Mapping[str, Any]: ...
    async def hook_status(self, workspace_id: str) -> Mapping[str, Any]: ...
    async def set_hook_trust(self, workspace_id: str, trusted: bool) -> Mapping[str, Any]: ...
    async def reload_hooks(self, workspace_id: str) -> Mapping[str, Any]: ...
    async def hook_audit(self, workspace_id: str) -> Sequence[Mapping[str, Any]]: ...
    async def session_memory_settings(
        self, thread_id: str, patch: dict[str, Any] | None = None
    ) -> Mapping[str, Any]: ...

    async def list_profiles(self) -> Mapping[str, Any]: ...
    async def create_profile(self, values: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def update_profile(self, name: str, values: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def test_profile(self, name: str) -> Mapping[str, Any]: ...
    async def select_profile(self, name: str) -> Mapping[str, Any]: ...
    async def delete_profile(self, name: str) -> Mapping[str, Any]: ...
    async def list_mcp(self, workspace_id: str) -> Mapping[str, Any]: ...
    async def add_mcp_server(
        self, workspace_id: str, name: str, scope: str, config: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    async def remove_mcp_server(
        self, workspace_id: str, name: str, scope: str
    ) -> Mapping[str, Any]: ...
    async def set_mcp_trust(self, workspace_id: str, trusted: bool) -> Mapping[str, Any]: ...
    async def reload_mcp(self, workspace_id: str) -> Mapping[str, Any]: ...
    async def list_skills(self, workspace_id: str) -> Sequence[Mapping[str, Any]]: ...
    async def activate_skill(self, thread_id: str, name: str) -> Mapping[str, Any]: ...
    async def list_memory(
        self, workspace_id: str, scope: str | None, query: str | None
    ) -> Sequence[Mapping[str, Any]]: ...
    async def memory_detail(self, workspace_id: str, memory_id: str) -> Mapping[str, Any]: ...
    async def memory_settings(self) -> Mapping[str, Any]: ...
    async def update_memory_settings(
        self, workspace_id: str, patch: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    async def remember(self, workspace_id: str, scope: str, text: str) -> Mapping[str, Any]: ...
    async def correct_memory(
        self, workspace_id: str, memory_id: str, text: str
    ) -> Mapping[str, Any]: ...
    async def confirm_memory(self, workspace_id: str, memory_id: str) -> Mapping[str, Any]: ...
    async def pin_memory(
        self, workspace_id: str, memory_id: str, pinned: bool
    ) -> Mapping[str, Any]: ...
    async def forget_memory(self, workspace_id: str, memory_id: str) -> Mapping[str, Any]: ...
    async def git_status(self, workspace_id: str) -> Mapping[str, Any]: ...
    async def symbols(self, workspace_id: str, query: str) -> Sequence[Mapping[str, Any]]: ...
    async def analyze(self, workspace_id: str) -> Mapping[str, Any]: ...
    async def doctor(self, workspace_id: str) -> Mapping[str, Any]: ...
    async def reviewer_status(self) -> Mapping[str, Any]: ...
    async def configure_reviewer(self, values: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def test_reviewer(self) -> Mapping[str, Any]: ...
    async def remove_reviewer(self) -> Mapping[str, Any]: ...
