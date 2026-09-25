"""本机消息附件的字节流入口；认证仍使用现有浏览器 Cookie 与 CSRF。"""

from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..host.attachments import AttachmentStore, AttachmentTooLargeError
from .contracts import WebHostProtocol
from .responses import call
from .security import require_session


class AttachmentView(BaseModel):
    id: str
    name: str
    size: int
    path: str
    kind: Literal["text", "binary"]
    media_type: str


def _require_attachment_mutation(request: Request) -> None:
    """文件体不是 JSON，因此单独验证同一会话和 CSRF，而不改变 JSON API 约束。"""
    require_session(request)
    supplied = request.headers.get("x-csrf-token", "")
    expected = request.app.state.csrf_token
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="CSRF token required")


def create_attachment_router(host: WebHostProtocol, store: AttachmentStore) -> APIRouter:
    """将上传、读取引用和取消操作绑定到当前本机宿主。"""
    router = APIRouter(prefix="/api/threads/{thread_id}/attachments")

    @router.post("", status_code=201, dependencies=[Depends(_require_attachment_mutation)])
    async def upload(
        request: Request, thread_id: str, name: str = Query(min_length=1)
    ) -> AttachmentView:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/octet-stream":
            raise HTTPException(status_code=415, detail="Binary upload required")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) < 0:
                    raise ValueError("negative")
                if int(length) > store.max_bytes:
                    raise HTTPException(status_code=413, detail="Attachment too large")
            except ValueError as error:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from error
        try:
            async with await host.session_guard(thread_id):
                await call(host.thread_snapshot(thread_id))
            # 网络上传不占用会话锁；停止和删除不能被慢速客户端阻塞。
            record = await store.save_stream(thread_id, name, request.stream())
            async with await host.session_guard(thread_id):
                try:
                    await call(host.thread_snapshot(thread_id))
                except BaseException:
                    # 会话可能在上传途中被删除；移除刚落盘的孤立文件。
                    await store.discard(thread_id, record.id)
                    raise
        except AttachmentTooLargeError as error:
            raise HTTPException(status_code=413, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return AttachmentView.model_validate(record.public())

    @router.get("/{attachment_id}", dependencies=[Depends(require_session)])
    async def metadata(thread_id: str, attachment_id: str) -> AttachmentView:
        await call(host.thread_snapshot(thread_id))
        try:
            record = store.resolve(thread_id, attachment_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Attachment not found") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return AttachmentView.model_validate(record.public())

    @router.delete("/{attachment_id}", dependencies=[Depends(_require_attachment_mutation)])
    async def discard(thread_id: str, attachment_id: str) -> dict[str, Any]:
        await call(host.thread_snapshot(thread_id))
        try:
            return {"discarded": await store.discard(thread_id, attachment_id)}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    return router


__all__ = ["AttachmentView", "create_attachment_router"]
