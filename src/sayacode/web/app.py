"""SAYACODE 本机 Web API；运行和持久化均由注入的宿主负责。"""

from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from ..host.attachments import AttachmentStore
from .attachments import create_attachment_router
from .contracts import (
    ApiModel,
    DirectoryListingView,
    EventEnvelope,
    QueuedMessageView,
    RunReceipt,
    SessionDeletionPreview,
    SessionDeletionResult,
    SessionView,
    SettingsView,
    StatusView,
    TaskView,
    ThreadSnapshot,
    WebHostProtocol,
    WorkspaceView,
)
from .responses import call as _call
from .responses import view as _view
from .security import (
    _COOKIE_NAME,
    check_local_request,
    new_browser_credentials,
    require_mutation,
    require_session,
    verify_launch_token,
)

_TRUST = Literal["read_only", "ask", "workspace_auto", "full"]
_TASK_ACTION = Literal["followup", "stop", "resume", "wait", "diff", "apply", "cleanup"]
_CURSOR = re.compile(r"^([A-Za-z0-9_-]{8,64}):([0-9]+)$")


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthRequest(_RequestModel):
    token: str = Field(min_length=1)


class WorkspaceCreate(_RequestModel):
    path: str = Field(min_length=1)
    name: str | None = None


class RenameRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=160)


class SessionCreate(_RequestModel):
    title: str | None = Field(default=None, max_length=160)


class ThreadRename(_RequestModel):
    title: str = Field(min_length=1, max_length=160)


class TrustChange(_RequestModel):
    trust_level: _TRUST


class ModelChange(_RequestModel):
    profile_name: str = Field(min_length=1)


class QueueMessageRequest(_RequestModel):
    message: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    message_id: str | None = None


class QueueEditRequest(_RequestModel):
    message: str = Field(min_length=1)


class RunRequest(_RequestModel):
    message: str = Field(min_length=1)


class ApprovalDecision(_RequestModel):
    type: Literal["approve", "reject"]
    message: str | None = None


class ApprovalGrant(_RequestModel):
    index: int = Field(ge=0)
    tool_name: str = Field(min_length=1)


class ApprovalRequest(_RequestModel):
    checkpoint_id: str = Field(min_length=1)
    decisions: list[ApprovalDecision] = Field(min_length=1)
    grants: list[ApprovalGrant] = Field(default_factory=list)


class TaskSpawn(_RequestModel):
    parent_thread_id: str = Field(min_length=1)
    role: Literal["builder", "planner", "reviewer"]
    prompt: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=160)
    worktree_enabled: bool | None = None


class TaskActionRequest(_RequestModel):
    message: str | None = None


class SettingsPatch(_RequestModel):
    language: Literal["auto", "zh", "en"] | None = None
    default_trust: _TRUST | None = None
    active_profile: str | None = None
    memory_enabled: bool | None = None
    output_limit_bytes: int | None = Field(default=None, gt=0)
    task_notice_limit_bytes: int | None = Field(default=None, gt=0)
    max_consecutive_wakes: int | None = Field(default=None, gt=0)
    shutdown_grace_seconds: float | None = Field(default=None, gt=0)


class TaskActionResult(ApiModel):
    task: TaskView | None = None
    diff: str | None = None
    status: str | None = None
    message: str | None = None
    result: str | None = None


def create_web_app(
    host: WebHostProtocol, static_dir: Path, attachments: AttachmentStore | None = None
) -> FastAPI:
    """创建只允许本机浏览器连接的 Web 应用。

    浏览器在 `/#token=...` 取得一次启动令牌，换取进程内 Cookie 和 CSRF 值。
    HTTP 与 SSE 的连接都不拥有 Agent 运行任务；关闭页面不停止图执行。
    """
    root = static_dir.expanduser().resolve()
    index = root / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"WebUI static index is missing: {index}")

    app = FastAPI(title="SAYACODE local API", docs_url=None, redoc_url=None, openapi_url=None)
    launch_token, browser_session, csrf_token = new_browser_credentials()
    app.state.launch_token = launch_token
    app.state.browser_session = browser_session
    app.state.csrf_token = csrf_token
    if attachments is not None:
        app.include_router(create_attachment_router(host, attachments))

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Response:
        try:
            check_local_request(request)
        except HTTPException as error:
            return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # 入口和深链始终取新 HTML，避免重建后仍引用已移除的旧哈希 chunk。
            response.headers["Cache-Control"] = "no-cache"
        return response

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        try:
            installed_version = version("sayacode")
        except PackageNotFoundError:
            installed_version = "dev"
        return {"ok": True, "version": installed_version}

    @app.post("/api/auth")
    async def authenticate(request: Request, body: AuthRequest, response: Response) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="JSON request required")
        verify_launch_token(request, body.token)
        response.set_cookie(
            _COOKIE_NAME,
            browser_session,
            httponly=True,
            secure=False,  # 仅支持本机 HTTP；跨网卡绑定由入口和中间件共同拒绝。
            samesite="strict",
            path="/",
        )
        return {"csrf_token": csrf_token}

    @app.get("/api/auth", dependencies=[Depends(require_session)])
    async def current_auth() -> dict[str, str]:
        return {"csrf_token": csrf_token}

    @app.get("/api/status", dependencies=[Depends(require_session)])
    async def status() -> StatusView:
        return _view(StatusView, await _call(host.status()))

    @app.get("/api/settings", dependencies=[Depends(require_session)])
    async def settings() -> SettingsView:
        return _view(SettingsView, await _call(host.settings()))

    @app.patch("/api/settings", dependencies=[Depends(require_mutation)])
    async def update_settings(body: SettingsPatch) -> SettingsView:
        patch = body.model_dump(exclude_unset=True)
        if not patch or any(value is None for value in patch.values()):
            raise HTTPException(status_code=422, detail="Non-empty settings patch required")
        return _view(SettingsView, await _call(host.update_settings(patch)))

    @app.get("/api/workspaces", dependencies=[Depends(require_session)])
    async def workspaces() -> dict[str, list[WorkspaceView]]:
        rows = await _call(host.list_workspaces())
        return {"workspaces": [_view(WorkspaceView, row) for row in rows]}

    @app.get("/api/directories", dependencies=[Depends(require_session)])
    async def directories(path: str | None = None) -> DirectoryListingView:
        return _view(DirectoryListingView, await _call(host.browse_directories(path)))

    @app.post("/api/workspaces", dependencies=[Depends(require_mutation)])
    async def create_workspace(body: WorkspaceCreate) -> WorkspaceView:
        return _view(WorkspaceView, await _call(host.create_workspace(body.path, body.name)))

    @app.patch("/api/workspaces/{workspace_id}", dependencies=[Depends(require_mutation)])
    async def rename_workspace(workspace_id: str, body: RenameRequest) -> WorkspaceView:
        return _view(WorkspaceView, await _call(host.rename_workspace(workspace_id, body.name)))

    @app.get("/api/workspaces/{workspace_id}/sessions", dependencies=[Depends(require_session)])
    async def sessions(workspace_id: str) -> dict[str, list[SessionView]]:
        rows = await _call(host.list_sessions(workspace_id))
        return {"sessions": [_view(SessionView, row) for row in rows]}

    @app.post("/api/workspaces/{workspace_id}/sessions", dependencies=[Depends(require_mutation)])
    async def create_session(workspace_id: str, body: SessionCreate) -> SessionView:
        return _view(SessionView, await _call(host.create_session(workspace_id, body.title)))

    @app.patch("/api/threads/{thread_id}", dependencies=[Depends(require_mutation)])
    async def rename_thread(thread_id: str, body: ThreadRename) -> SessionView:
        return _view(SessionView, await _call(host.rename_thread(thread_id, body.title)))

    @app.get("/api/threads/{thread_id}/delete-preview", dependencies=[Depends(require_session)])
    async def session_delete_preview(thread_id: str) -> SessionDeletionPreview:
        return _view(SessionDeletionPreview, await _call(host.session_deletion_preview(thread_id)))

    @app.delete("/api/threads/{thread_id}", dependencies=[Depends(require_mutation)])
    async def delete_session(thread_id: str) -> SessionDeletionResult:
        return _view(SessionDeletionResult, await _call(host.delete_session(thread_id)))

    @app.patch("/api/threads/{thread_id}/trust", dependencies=[Depends(require_mutation)])
    async def set_trust(thread_id: str, body: TrustChange) -> dict[str, str]:
        result = await _call(host.set_trust(thread_id, body.trust_level))
        return {"trust_level": str(result["trust_level"])}

    @app.patch("/api/threads/{thread_id}/model", dependencies=[Depends(require_mutation)])
    async def set_thread_model(thread_id: str, body: ModelChange) -> ThreadSnapshot:
        return _view(ThreadSnapshot, await _call(host.set_thread_model(thread_id, body.profile_name)))

    @app.get("/api/threads/{thread_id}/queue", dependencies=[Depends(require_session)])
    async def queued_messages(thread_id: str) -> dict[str, list[QueuedMessageView]]:
        rows = await _call(host.queued_messages(thread_id))
        return {"messages": [_view(QueuedMessageView, item) for item in rows]}

    @app.post("/api/threads/{thread_id}/queue", status_code=202,
              dependencies=[Depends(require_mutation)])
    async def queue_message(thread_id: str, body: QueueMessageRequest) -> QueuedMessageView:
        return _view(
            QueuedMessageView,
            await _call(
                host.queue_message(
                    thread_id, body.message, attachment_ids=body.attachment_ids,
                    message_id=body.message_id,
                )
            ),
        )

    @app.patch("/api/threads/{thread_id}/queue/{message_id}",
               dependencies=[Depends(require_mutation)])
    async def edit_queue(thread_id: str, message_id: str, body: QueueEditRequest) -> QueuedMessageView:
        return _view(
            QueuedMessageView,
            await _call(host.edit_queued_message(thread_id, message_id, body.message)),
        )

    @app.post("/api/threads/{thread_id}/queue/{message_id}/steer",
              dependencies=[Depends(require_mutation)])
    async def steer_queue(thread_id: str, message_id: str) -> QueuedMessageView:
        return _view(
            QueuedMessageView,
            await _call(host.promote_queued_message(thread_id, message_id)),
        )

    @app.delete("/api/threads/{thread_id}/queue/{message_id}",
                dependencies=[Depends(require_mutation)])
    async def remove_queue(thread_id: str, message_id: str) -> dict[str, bool]:
        await _call(host.remove_queued_message(thread_id, message_id))
        return {"deleted": True}

    @app.get("/api/threads/{thread_id}/snapshot", dependencies=[Depends(require_session)])
    async def snapshot(thread_id: str) -> ThreadSnapshot:
        return _view(ThreadSnapshot, await _call(host.thread_snapshot(thread_id)))

    @app.get("/api/threads/{thread_id}/tasks", dependencies=[Depends(require_session)])
    async def thread_tasks(thread_id: str) -> dict[str, list[TaskView]]:
        rows = await _call(host.list_thread_tasks(thread_id))
        return {"tasks": [_view(TaskView, row) for row in rows]}

    @app.post("/api/threads/{thread_id}/runs", status_code=202, dependencies=[Depends(require_mutation)])
    async def start_run(thread_id: str, body: RunRequest) -> RunReceipt:
        return _view(RunReceipt, await _call(host.start_run(thread_id, body.message)))

    @app.post("/api/threads/{thread_id}/runs/stop", status_code=202,
              dependencies=[Depends(require_mutation)])
    async def stop_run(thread_id: str) -> dict[str, Any]:
        return dict(await _call(host.stop_session_tree(thread_id)))

    @app.post("/api/threads/{thread_id}/runs/resume", status_code=202,
              dependencies=[Depends(require_mutation)])
    async def resume_run(thread_id: str) -> RunReceipt:
        return _view(RunReceipt, await _call(host.resume_run(thread_id)))

    @app.post(
        "/api/threads/{thread_id}/approvals",
        status_code=202,
        dependencies=[Depends(require_mutation)],
    )
    async def decide_approval(thread_id: str, body: ApprovalRequest) -> RunReceipt:
        decisions = [item.model_dump(exclude_none=True) for item in body.decisions]
        grants = [item.model_dump() for item in body.grants]
        result = await _call(host.decide_approval(thread_id, body.checkpoint_id, decisions, grants))
        return _view(RunReceipt, result)

    @app.get("/api/tasks", dependencies=[Depends(require_session)])
    async def tasks(workspace_id: str | None = None) -> dict[str, list[TaskView]]:
        rows = await _call(host.list_tasks(workspace_id))
        return {"tasks": [_view(TaskView, row) for row in rows]}

    @app.post("/api/tasks", status_code=202, dependencies=[Depends(require_mutation)])
    async def spawn_task(body: TaskSpawn) -> TaskView:
        result = await _call(
            host.spawn_task(
                body.parent_thread_id,
                body.role,
                body.prompt,
                body.title,
                body.worktree_enabled,
            )
        )
        return _view(TaskView, result)

    @app.post("/api/tasks/{task_id}/{action}", dependencies=[Depends(require_mutation)])
    async def task_action(
        task_id: str, action: _TASK_ACTION, body: TaskActionRequest
    ) -> TaskActionResult:
        if action == "followup" and not (body.message or "").strip():
            raise HTTPException(status_code=422, detail="Follow-up message required")
        result = await _call(host.task_action(task_id, action, body.model_dump(exclude_none=True)))
        return _view(TaskActionResult, result)

    @app.get("/api/events", dependencies=[Depends(require_session)])
    async def events(
        workspace_id: str | None = None,
        after: str | None = Query(default=None),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        # EventSource 自动重连时，浏览器新游标必须覆盖初次订阅 URL 的旧 after。
        cursor = last_event_id if last_event_id else after
        instance_id: str | None = None
        sequence: int | None = None
        if cursor is not None:
            match = _CURSOR.fullmatch(cursor)
            if match is None:
                raise HTTPException(status_code=400, detail="Invalid event cursor")
            instance_id, raw_sequence = match.groups()
            sequence = int(raw_sequence)

        async def stream() -> Any:
            async for raw in host.subscribe(workspace_id, sequence, instance_id):
                event = _view(EventEnvelope, raw)
                payload = json.dumps(event.model_dump(), ensure_ascii=False, separators=(",", ":"))
                event_id = (
                    ""
                    if event.type in {"stream.ready", "stream.resync_required"}
                    else f"id: {event.instance_id}:{event.seq}\n"
                )
                yield f"{event_id}event: message\ndata: {payload}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    from .products import register_product_routes
    from .session_operations import register_session_routes

    register_product_routes(app, host)
    register_session_routes(app, host)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Unknown API route")
        candidate = (root / path).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    return app


__all__ = ["create_web_app"]
