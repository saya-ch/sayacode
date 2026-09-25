"""消息附件只在本机暂存，绑定、清理和上传鉴权均有行为验证。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from time import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from sayacode.host.attachments import AttachmentStore, AttachmentTooLargeError
from sayacode.web.attachments import create_attachment_router


async def _chunks(*values: bytes):
    for value in values:
        yield value


async def test_attachment_keeps_unicode_text_locally_and_binds_once(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "state")
    record = await store.save_stream("session-中文", "算法 笔记.py", _chunks(b"print(1)\n"))
    assert record.kind == "text"
    assert Path(record.path).read_bytes() == b"print(1)\n"
    assert Path(record.path).is_relative_to(tmp_path / "state" / "attachments")
    assert store.resolve("session-中文", record.id).name == "算法 笔记.py"
    with pytest.raises(KeyError):
        store.resolve("another-thread", record.id)

    bound = await store.bind("session-中文", [record.id], "queued-message-1")
    assert bound[0].bound_to == "queued-message-1"
    assert (await store.bind("session-中文", [record.id], "queued-message-1"))[
        0
    ].path == record.path
    with pytest.raises(RuntimeError, match="其他消息"):
        await store.bind("session-中文", [record.id], "queued-message-2")
    with pytest.raises(RuntimeError, match="已发送"):
        await store.discard("session-中文", record.id)
    await store.release_bindings("session-中文", [record.id], "queued-message-1")
    assert store.resolve("session-中文", record.id).bound_to is None
    assert await store.discard("session-中文", record.id) is True


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.txt",
        "folder/file.py",
        "x\\y.py",
        "CON.txt",
        "record.json",
        "bad?.txt",
        "trailing. ",
    ],
)
async def test_attachment_rejects_unsafe_filename(tmp_path: Path, filename: str) -> None:
    store = AttachmentStore(tmp_path / "state")
    with pytest.raises(ValueError, match="文件名"):
        await store.save_stream("session-1", filename, _chunks(b"x"))
    assert not (tmp_path / "state" / "attachments").exists()


async def test_attachment_limit_and_failed_upload_leave_no_payload(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "state", max_bytes=5)
    with pytest.raises(AttachmentTooLargeError):
        await store.save_stream("session-1", "data.bin", _chunks(b"123", b"456"))
    assert list((tmp_path / "state" / "attachments").rglob("*.bin")) == []
    assert list((tmp_path / "state" / "attachments").rglob("*.part")) == []
    with pytest.raises(ValueError, match="UTF-8"):
        await store.save_stream("session-1", "picture.png", _chunks(b"\x89PNG\x00"))
    assert list((tmp_path / "state" / "attachments").rglob("*.png")) == []


async def test_cleanup_only_removes_expired_unbound_entries(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "state")
    pending = await store.save_stream("session-1", "draft.txt", _chunks(b"pending"))
    bound = await store.save_stream("session-1", "keep.txt", _chunks(b"bound"))
    await store.bind("session-1", [bound.id], "message-1")
    outside = tmp_path / "unrelated.txt"
    outside.write_text("keep", encoding="utf-8")
    past = time() - 48 * 60 * 60
    for record in (pending, bound):
        os.utime(Path(record.path).parent, (past, past))

    assert await store.cleanup_orphans() == 1
    with pytest.raises(KeyError):
        store.resolve("session-1", pending.id)
    assert store.resolve("session-1", bound.id).path == bound.path
    assert outside.read_text(encoding="utf-8") == "keep"


async def test_session_cleanup_only_removes_its_own_known_attachments(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "state")
    own = await store.save_stream("session-1", "own.txt", _chunks(b"own"))
    other = await store.save_stream("session-2", "other.txt", _chunks(b"other"))
    await store.bind("session-1", [own.id], "message-1")
    unknown = Path(own.path).parent / "unrecognized.bin"
    unknown.write_bytes(b"outside this feature")
    assert await store.remove_thread("session-1") == 0
    assert Path(own.path).exists()
    unknown.unlink()
    assert await store.remove_thread("session-1") == 1
    assert not Path(own.path).exists()
    assert Path(other.path).read_bytes() == b"other"


class _Host:
    def __init__(self) -> None:
        self.snapshots = 0
        self._lock = asyncio.Lock()

    async def session_guard(self, thread_id: str) -> asyncio.Lock:
        return self._lock

    async def thread_snapshot(self, thread_id: str) -> dict[str, Any]:
        self.snapshots += 1
        if thread_id != "session-1":
            raise KeyError(thread_id)
        return {"thread_id": thread_id}


async def test_slow_upload_does_not_hold_session_lock(tmp_path: Path) -> None:
    host = _Host()
    store = AttachmentStore(tmp_path / "state")
    app = FastAPI()
    app.state.browser_session = "session-cookie"
    app.state.csrf_token = "csrf-value"
    app.include_router(create_attachment_router(host, store))  # type: ignore[arg-type]
    first_chunk = asyncio.Event()
    finish_upload = asyncio.Event()

    async def slow_content():
        yield b"first"
        first_chunk.set()
        await finish_upload.wait()
        yield b"second"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        client.cookies.set("sayacode_session", "session-cookie")
        upload = asyncio.create_task(
            client.post(
                "/api/threads/session-1/attachments",
                params={"name": "slow.txt"},
                content=slow_content(),
                headers={
                    "X-CSRF-Token": "csrf-value",
                    "Content-Type": "application/octet-stream",
                },
            )
        )
        try:
            await asyncio.wait_for(first_chunk.wait(), 1)
            async with asyncio.timeout(0.2):
                async with host._lock:
                    pass
        finally:
            finish_upload.set()
        response = await upload
    assert response.status_code == 201
    assert Path(response.json()["path"]).read_text(encoding="utf-8") == "firstsecond"


async def test_attachment_route_requires_session_and_csrf_and_returns_local_reference(
    tmp_path: Path,
) -> None:
    host = _Host()
    store = AttachmentStore(tmp_path / "state", max_bytes=8)
    app = FastAPI()
    app.state.browser_session = "session-cookie"
    app.state.csrf_token = "csrf-value"
    app.include_router(create_attachment_router(host, store))  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    url = "/api/threads/session-1/attachments"
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        forbidden = await client.post(url, params={"name": "note.txt"}, content=b"hello")
        assert forbidden.status_code == 401
        client.cookies.set("sayacode_session", "session-cookie")
        forbidden = await client.post(url, params={"name": "note.txt"}, content=b"hello")
        assert forbidden.status_code == 403
        headers = {"X-CSRF-Token": "csrf-value", "Content-Type": "application/octet-stream"}
        wrong_type = await client.post(
            url,
            params={"name": "note.txt"},
            content=b"hello",
            headers={"X-CSRF-Token": "csrf-value"},
        )
        assert wrong_type.status_code == 415
        too_large = await client.post(
            url, params={"name": "note.txt"}, content=b"123456789", headers=headers
        )
        assert too_large.status_code == 413
        uploaded = await client.post(
            url, params={"name": "笔记.txt"}, content=b"hello", headers=headers
        )
        assert uploaded.status_code == 201
        body = uploaded.json()
        assert body["name"] == "笔记.txt"
        assert body["size"] == 5
        assert body["kind"] == "text"
        assert Path(body["path"]).read_bytes() == b"hello"
        attachment_url = f"{url}/{body['id']}"
        assert (await client.get(attachment_url)).json()["id"] == body["id"]
        assert (await client.delete(attachment_url)).status_code == 403
        assert (
            await client.delete(attachment_url, headers={"X-CSRF-Token": "csrf-value"})
        ).json() == {"discarded": True}
        assert not Path(body["path"]).exists()
        assert (await client.get(attachment_url)).status_code == 404
        assert host.snapshots >= 3
