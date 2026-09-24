"""模型、扩展、记忆和诊断的类型化 HTTP 路由。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .contracts import ApiModel, WebHostProtocol
from .responses import call, view
from .security import require_mutation, require_session

_PROTOCOL = Literal[
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "gemini_generate_content",
    "ollama_native_chat",
]
_MEMORY_SCOPE = Literal["user", "project"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileView(ApiModel):
    name: str
    protocol: str
    base_url: str
    model_id: str
    context_length: int
    max_output_tokens: int
    has_api_key: bool = False
    summary_trigger_ratio: float | None = None


class ProfileCatalog(ApiModel):
    active_profile: str | None = None
    profiles: list[ProfileView] = Field(default_factory=list)


class ProfileInput(RequestModel):
    name: str | None = None
    protocol: _PROTOCOL
    base_url: str = Field(min_length=1)
    api_key: str | None
    model_id: str = Field(min_length=1)
    context_length: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class ProfilePatch(RequestModel):
    protocol: _PROTOCOL | None = None
    base_url: str | None = None
    api_key: str | None = None
    model_id: str | None = None
    context_length: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    summary_trigger_ratio: float | None = Field(default=None, gt=0, lt=1)


class ProfileTest(ApiModel):
    profile: str
    ok: bool
    text: bool = False
    tool_calling: bool = False
    stream: bool = False
    errors: dict[str, str] = Field(default_factory=dict)


class ProfileSelection(ApiModel):
    active_profile: str | None = None


class McpServerView(ApiModel):
    name: str
    scope: str
    status: str
    tools: list[str] | None = None
    error: str | None = None


class McpCatalog(ApiModel):
    trusted_project: bool = False
    servers: list[McpServerView] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)


class McpServerInput(RequestModel):
    name: str = Field(min_length=1)
    scope: Literal["user", "project"]
    config: dict[str, Any]


class McpTrust(RequestModel):
    trusted: bool


class McpResult(ApiModel):
    status: str
    name: str | None = None
    trusted_project: bool | None = None
    error: str | None = None


class SkillView(ApiModel):
    name: str
    description: str
    source: str
    path: str
    active: bool | None = None


class SkillResult(ApiModel):
    name: str
    active: bool


class MemoryView(ApiModel):
    id: str
    scope: str
    subject: str
    text: str
    state: str
    pinned: bool = False
    updated_at: str | None = None


class MemoryEvidence(ApiModel):
    source_ref: str
    message_id: str
    role: str
    preview: str


class MemoryDetail(MemoryView):
    validity_reason: str | None = None
    evidence: list[MemoryEvidence] = Field(default_factory=list)


class MemorySettings(ApiModel):
    enabled: bool = False
    use: bool = True
    learn: str = "off"
    model_profile: str | None = None
    idle_seconds: float = 30.0
    context_ratio: float = 0.03
    max_context_tokens: int = 1600


class MemorySettingsPatch(RequestModel):
    enabled: bool | None = None
    use: bool | None = None
    learn: Literal["off", "explicit", "auto"] | None = None
    model_profile: str | None = None
    idle_seconds: float | None = Field(default=None, ge=0)
    context_ratio: float | None = Field(default=None, gt=0, le=1)
    max_context_tokens: int | None = Field(default=None, gt=0)


class MemoryInput(RequestModel):
    workspace_id: str = Field(min_length=1)
    scope: _MEMORY_SCOPE
    text: str = Field(min_length=1)


class MemoryCorrection(RequestModel):
    text: str = Field(min_length=1)


class MemoryPin(RequestModel):
    pinned: bool


class GitStatus(ApiModel):
    branch: str | None = None
    clean: bool = False
    staged: list[str] = Field(default_factory=list)
    unstaged: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    text: str | None = None


class SymbolView(ApiModel):
    name: str
    kind: str
    path: str
    line: int | None = None


class TextResult(ApiModel):
    text: str


class DoctorCheck(ApiModel):
    name: str
    status: str
    detail: str | None = None


class DoctorResult(ApiModel):
    ok: bool
    checks: list[DoctorCheck] = Field(default_factory=list)
    summary: str | None = None


class ReviewerStatus(ApiModel):
    configured: bool
    base_url: str | None = None
    model_id: str | None = None
    has_api_key: bool = False


class ReviewerInput(RequestModel):
    base_url: str = Field(min_length=1)
    api_key: str | None = None
    model_id: str = Field(min_length=1)


class ReviewerTest(ApiModel):
    ok: bool
    error: str | None = None


def _nonempty_patch(body: BaseModel) -> dict[str, Any]:
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=422, detail="Non-empty patch required")
    return patch


def _rows(model: type[BaseModel], values: Sequence[Mapping[str, Any]]) -> list[Any]:
    return [view(model, item) for item in values]


def register_product_routes(app: FastAPI, host: WebHostProtocol) -> None:
    """注册产品操作。路由只校验输入、投影响应并调用宿主。"""
    router = APIRouter(prefix="/api", dependencies=[Depends(require_session)])

    @router.get("/models")
    async def models() -> ProfileCatalog:
        return view(ProfileCatalog, await call(host.list_profiles()))

    @router.post("/models", dependencies=[Depends(require_mutation)])
    async def add_model(body: ProfileInput) -> ProfileView:
        return view(ProfileView, await call(host.create_profile(body.model_dump())))

    @router.patch("/models/{name}", dependencies=[Depends(require_mutation)])
    async def update_model(name: str, body: ProfilePatch) -> ProfileView:
        return view(ProfileView, await call(host.update_profile(name, _nonempty_patch(body))))

    @router.post("/models/{name}/test", dependencies=[Depends(require_mutation)])
    async def test_model(name: str) -> ProfileTest:
        return view(ProfileTest, await call(host.test_profile(name)))

    @router.post("/models/{name}/use", dependencies=[Depends(require_mutation)])
    async def use_model(name: str) -> ProfileSelection:
        return view(ProfileSelection, await call(host.select_profile(name)))

    @router.delete("/models/{name}", dependencies=[Depends(require_mutation)])
    async def delete_model(name: str) -> ProfileSelection:
        return view(ProfileSelection, await call(host.delete_profile(name)))

    @router.get("/workspaces/{workspace_id}/mcp")
    async def mcp(workspace_id: str) -> McpCatalog:
        return view(McpCatalog, await call(host.list_mcp(workspace_id)))

    @router.post("/workspaces/{workspace_id}/mcp/servers", dependencies=[Depends(require_mutation)])
    async def add_mcp(workspace_id: str, body: McpServerInput) -> McpResult:
        result = await call(host.add_mcp_server(workspace_id, body.name, body.scope, body.config))
        return view(McpResult, result)

    @router.delete(
        "/workspaces/{workspace_id}/mcp/servers/{name}", dependencies=[Depends(require_mutation)]
    )
    async def remove_mcp(
        workspace_id: str, name: str, scope: Literal["user", "project"] = Query(...)
    ) -> McpResult:
        return view(McpResult, await call(host.remove_mcp_server(workspace_id, name, scope)))

    @router.post("/workspaces/{workspace_id}/mcp/trust", dependencies=[Depends(require_mutation)])
    async def trust_mcp(workspace_id: str, body: McpTrust) -> McpResult:
        return view(McpResult, await call(host.set_mcp_trust(workspace_id, body.trusted)))

    @router.post("/workspaces/{workspace_id}/mcp/reload", dependencies=[Depends(require_mutation)])
    async def reload_mcp(workspace_id: str) -> McpResult:
        return view(McpResult, await call(host.reload_mcp(workspace_id)))

    @router.get("/workspaces/{workspace_id}/skills")
    async def skills(workspace_id: str) -> dict[str, list[SkillView]]:
        return {"skills": _rows(SkillView, await call(host.list_skills(workspace_id)))}

    @router.post("/threads/{thread_id}/skills/{name}/activate", dependencies=[Depends(require_mutation)])
    async def activate_skill(thread_id: str, name: str) -> SkillResult:
        return view(SkillResult, await call(host.activate_skill(thread_id, name)))

    @router.get("/memory")
    async def memories(
        workspace_id: str,
        scope: _MEMORY_SCOPE | None = None,
        query: str | None = None,
    ) -> dict[str, list[MemoryView]]:
        return {"records": _rows(MemoryView, await call(host.list_memory(workspace_id, scope, query)))}

    @router.get("/memory/settings")
    async def memory_settings() -> MemorySettings:
        return view(MemorySettings, await call(host.memory_settings()))

    @router.get("/memory/{memory_id}")
    async def memory_detail(memory_id: str, workspace_id: str) -> MemoryDetail:
        return view(MemoryDetail, await call(host.memory_detail(workspace_id, memory_id)))

    @router.patch("/memory/settings", dependencies=[Depends(require_mutation)])
    async def update_memory_settings(workspace_id: str, body: MemorySettingsPatch) -> MemorySettings:
        result = await call(host.update_memory_settings(workspace_id, _nonempty_patch(body)))
        return view(MemorySettings, result)

    @router.post("/memory", dependencies=[Depends(require_mutation)])
    async def remember(body: MemoryInput) -> MemoryView:
        return view(MemoryView, await call(host.remember(body.workspace_id, body.scope, body.text)))

    @router.patch("/memory/{memory_id}", dependencies=[Depends(require_mutation)])
    async def correct_memory(
        memory_id: str, workspace_id: str, body: MemoryCorrection
    ) -> MemoryView:
        return view(MemoryView, await call(host.correct_memory(workspace_id, memory_id, body.text)))

    @router.post("/memory/{memory_id}/confirm", dependencies=[Depends(require_mutation)])
    async def confirm_memory(memory_id: str, workspace_id: str) -> MemoryView:
        return view(MemoryView, await call(host.confirm_memory(workspace_id, memory_id)))

    @router.post("/memory/{memory_id}/pin", dependencies=[Depends(require_mutation)])
    async def pin_memory(memory_id: str, workspace_id: str, body: MemoryPin) -> MemoryView:
        return view(MemoryView, await call(host.pin_memory(workspace_id, memory_id, body.pinned)))

    @router.delete("/memory/{memory_id}", dependencies=[Depends(require_mutation)])
    async def forget_memory(memory_id: str, workspace_id: str) -> MemoryView:
        return view(MemoryView, await call(host.forget_memory(workspace_id, memory_id)))

    @router.get("/workspaces/{workspace_id}/git/status")
    async def git_status(workspace_id: str) -> GitStatus:
        return view(GitStatus, await call(host.git_status(workspace_id)))

    @router.get("/workspaces/{workspace_id}/symbols")
    async def symbols(workspace_id: str, query: str = "") -> dict[str, list[SymbolView]]:
        return {"symbols": _rows(SymbolView, await call(host.symbols(workspace_id, query)))}

    @router.get("/workspaces/{workspace_id}/analysis")
    async def analyze(workspace_id: str) -> TextResult:
        return view(TextResult, await call(host.analyze(workspace_id)))

    @router.get("/workspaces/{workspace_id}/doctor")
    async def doctor(workspace_id: str) -> DoctorResult:
        return view(DoctorResult, await call(host.doctor(workspace_id)))

    @router.get("/reviewer")
    async def reviewer() -> ReviewerStatus:
        return view(ReviewerStatus, await call(host.reviewer_status()))

    @router.put("/reviewer", dependencies=[Depends(require_mutation)])
    async def configure_reviewer(body: ReviewerInput) -> ReviewerStatus:
        return view(ReviewerStatus, await call(host.configure_reviewer(body.model_dump())))

    @router.post("/reviewer/test", dependencies=[Depends(require_mutation)])
    async def test_reviewer() -> ReviewerTest:
        return view(ReviewerTest, await call(host.test_reviewer()))

    @router.delete("/reviewer", dependencies=[Depends(require_mutation)])
    async def remove_reviewer() -> ReviewerStatus:
        return view(ReviewerStatus, await call(host.remove_reviewer()))

    app.include_router(router)
