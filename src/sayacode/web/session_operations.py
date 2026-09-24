"""检查点、追踪、工具目录与 Hook 的类型化 Web 路由。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from .contracts import ApiModel, WebHostProtocol
from .responses import call, view
from .security import require_mutation, require_session


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckpointView(ApiModel):
    checkpoint_id: str
    next: list[str] = Field(default_factory=list)
    message_count: int = 0
    created_at: str | None = None


class CompactRequest(RequestModel):
    focus: str | None = None


class CompactResult(ApiModel):
    compacted: bool


class RewindRequest(RequestModel):
    checkpoint_id: str = Field(min_length=1)


class RewindResult(ApiModel):
    rewound: bool
    checkpoint_id: str
    fork_checkpoint_id: str | None = None


class TraceView(ApiModel):
    id: str
    at: str | None = None
    event: str
    thread_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    details: dict[str, Any] | None = None


class ToolView(ApiModel):
    name: str
    description: str = ""


class GrantResult(ApiModel):
    cleared: bool


class HookView(ApiModel):
    event: str
    name: str
    source: str
    blocking: bool
    timeout: float


class HookStatus(ApiModel):
    workspace: str
    project_trusted: bool
    user_hooks: int = 0
    project_hooks: int = 0
    warnings: list[str] = Field(default_factory=list)
    hooks: list[HookView] = Field(default_factory=list)


class HookTrust(RequestModel):
    trusted: bool


class HookAudit(ApiModel):
    id: str
    at: str | None = None
    event: str
    name: str
    source: str
    returncode: int
    blocked: bool


class SessionMemorySettings(ApiModel):
    thread_id: str
    use_override: bool | None = None
    learn_override: str | None = None
    use: bool
    learn: str


class SessionMemoryPatch(RequestModel):
    use: bool | None = None
    learn: Literal["off", "explicit", "auto"] | None = None


def register_session_routes(app: FastAPI, host: WebHostProtocol) -> None:
    """HTTP 层只校验参数与投影；状态变更调用宿主的原生图操作。"""
    router = APIRouter(prefix="/api", dependencies=[Depends(require_session)])

    @router.get("/threads/{thread_id}/checkpoints")
    async def checkpoints(thread_id: str) -> dict[str, list[CheckpointView]]:
        rows = await call(host.list_checkpoints(thread_id))
        return {"checkpoints": [view(CheckpointView, row) for row in rows]}

    @router.post("/threads/{thread_id}/compact", dependencies=[Depends(require_mutation)])
    async def compact(thread_id: str, body: CompactRequest) -> CompactResult:
        return view(CompactResult, await call(host.compact_thread(thread_id, body.focus)))

    @router.post("/threads/{thread_id}/rewind", dependencies=[Depends(require_mutation)])
    async def rewind(thread_id: str, body: RewindRequest) -> RewindResult:
        return view(RewindResult, await call(host.rewind_thread(thread_id, body.checkpoint_id)))

    @router.get("/threads/{thread_id}/trace")
    async def trace(thread_id: str, run_id: str | None = None) -> dict[str, list[TraceView]]:
        rows = await call(host.trace_thread(thread_id, run_id))
        return {"activity": [view(TraceView, row) for row in rows]}

    @router.get("/threads/{thread_id}/tools")
    async def tools(thread_id: str) -> dict[str, list[ToolView]]:
        rows = await call(host.list_thread_tools(thread_id))
        return {"tools": [view(ToolView, row) for row in rows]}

    @router.delete("/threads/{thread_id}/approvals/grants", dependencies=[Depends(require_mutation)])
    async def clear_grants(thread_id: str) -> GrantResult:
        return view(GrantResult, await call(host.clear_thread_grants(thread_id)))

    @router.get("/threads/{thread_id}/memory/settings")
    async def memory_settings(thread_id: str) -> SessionMemorySettings:
        return view(SessionMemorySettings, await call(host.session_memory_settings(thread_id)))

    @router.patch(
        "/threads/{thread_id}/memory/settings", dependencies=[Depends(require_mutation)]
    )
    async def update_memory_settings(
        thread_id: str, body: SessionMemoryPatch
    ) -> SessionMemorySettings:
        return view(
            SessionMemorySettings,
            await call(host.session_memory_settings(thread_id, body.model_dump(exclude_unset=True))),
        )

    @router.get("/workspaces/{workspace_id}/hooks")
    async def hooks(workspace_id: str) -> HookStatus:
        return view(HookStatus, await call(host.hook_status(workspace_id)))

    @router.patch("/workspaces/{workspace_id}/hooks/trust", dependencies=[Depends(require_mutation)])
    async def trust_hooks(workspace_id: str, body: HookTrust) -> HookStatus:
        return view(HookStatus, await call(host.set_hook_trust(workspace_id, body.trusted)))

    @router.post("/workspaces/{workspace_id}/hooks/reload", dependencies=[Depends(require_mutation)])
    async def reload_hooks(workspace_id: str) -> HookStatus:
        return view(HookStatus, await call(host.reload_hooks(workspace_id)))

    @router.get("/workspaces/{workspace_id}/hooks/audit")
    async def hooks_audit(workspace_id: str) -> dict[str, list[HookAudit]]:
        rows = await call(host.hook_audit(workspace_id))
        return {"activity": [view(HookAudit, row) for row in rows]}

    app.include_router(router)


__all__ = ["register_session_routes"]
