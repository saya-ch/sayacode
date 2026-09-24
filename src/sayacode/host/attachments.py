"""浏览器消息附件的本机暂存；不复制会话历史，也不代替模型文件工具。"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import mimetypes
import os
import re
from collections.abc import AsyncIterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import time
from uuid import uuid4

MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
PENDING_TTL_SECONDS = 24 * 60 * 60
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_THREAD_DIRECTORY = re.compile(r"^[0-9a-f]{40}$")
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class AttachmentTooLargeError(ValueError):
    """上传超过本机附件上限。"""


def _filename(value: str) -> str:
    if (
        not value
        or len(value) > 160
        or value in {".", ".."}
        or value != value.strip()
        or value.endswith(".")
        or value.lower() in {"upload.part", "record.json", "record.tmp"}
        or _BAD_FILENAME.search(value)
        or _WINDOWS_DEVICE.match(value)
    ):
        raise ValueError("附件文件名无效")
    return value


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    id: str
    thread_id: str
    name: str
    size: int
    path: str
    kind: str
    media_type: str
    created_at: str
    bound_to: str | None = None

    def public(self) -> dict[str, str | int]:
        """只返回输入框需要的本机引用，不暴露内部绑定状态。"""
        return {
            key: value
            for key, value in asdict(self).items()
            if key in {"id", "name", "size", "path", "kind", "media_type"}
        }


class AttachmentStore:
    """附件目录由 SAYACODE_HOME 派生；ID 和线程摘要都不接受路径片段。"""

    def __init__(self, home: str | Path, *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> None:
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / "attachments"
        self.max_bytes = max_bytes
        self._lock = asyncio.Lock()

    def _ensure_root(self) -> None:
        _private_dir(self.home)
        if self.root.is_symlink():
            raise PermissionError("附件目录不能是符号链接")
        _private_dir(self.root)
        if self.root.resolve() != self.root:
            raise PermissionError("附件目录越过应用数据根目录")

    def _thread_path(self, thread_id: str) -> Path:
        if not thread_id or len(thread_id) > 256 or "\x00" in thread_id:
            raise ValueError("线程标识无效")
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:40]
        return self.root / digest

    def _entry_path(self, thread_id: str, attachment_id: str) -> Path:
        if not _IDENTIFIER.fullmatch(attachment_id):
            raise ValueError("附件标识无效")
        return self._thread_path(thread_id) / attachment_id

    def _check_owned(self, path: Path) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise PermissionError("附件路径不属于本机附件目录")

    def _record(self, thread_id: str, attachment_id: str) -> AttachmentRecord:
        self._ensure_root()
        entry = self._entry_path(thread_id, attachment_id)
        self._check_owned(entry)
        manifest = entry / "record.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise KeyError("附件不存在")
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            name = _filename(raw["name"])
            if raw["id"] != attachment_id or raw["thread_id"] != thread_id:
                raise ValueError("附件归属不匹配")
            if raw.get("bound_to") is not None and not isinstance(raw["bound_to"], str):
                raise ValueError("附件绑定信息无效")
            path = entry / name
            self._check_owned(path)
            if not path.is_file() or path.stat().st_size != raw["size"]:
                raise ValueError("附件文件已变化")
            return AttachmentRecord(
                id=attachment_id,
                thread_id=thread_id,
                name=name,
                size=int(raw["size"]),
                path=str(path),
                kind=str(raw["kind"]),
                media_type=str(raw["media_type"]),
                created_at=str(raw["created_at"]),
                bound_to=raw.get("bound_to"),
            )
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise ValueError("附件记录无效") from error

    @staticmethod
    def _save_record(entry: Path, record: AttachmentRecord) -> None:
        temporary = entry / "record.tmp"
        manifest = entry / "record.json"
        temporary.write_text(json.dumps(asdict(record), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, manifest)

    async def save_stream(
        self, thread_id: str, filename: str, chunks: AsyncIterable[bytes]
    ) -> AttachmentRecord:
        """逐块落盘并限额；失败或取消时只删除本次创建的临时文件。"""
        name = _filename(filename)
        self._ensure_root()
        thread_path = self._thread_path(thread_id)
        self._check_owned(thread_path)
        _private_dir(thread_path)
        attachment_id = uuid4().hex
        entry = self._entry_path(thread_id, attachment_id)
        entry.mkdir(exist_ok=False)
        part = entry / "upload.part"
        path = entry / name
        size = 0
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with part.open("xb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ValueError("附件数据必须是字节流")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise AttachmentTooLargeError(f"附件超过 {self.max_bytes} 字节上限")
                    if b"\x00" in chunk:
                        raise ValueError("当前仅支持 UTF-8 文本附件")
                    try:
                        decoder.decode(chunk, final=False)
                    except UnicodeDecodeError as error:
                        raise ValueError("当前仅支持 UTF-8 文本附件") from error
                    await asyncio.to_thread(output.write, chunk)
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as error:
                raise ValueError("当前仅支持 UTF-8 文本附件") from error
            kind = "text"
            media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            os.replace(part, path)
            record = AttachmentRecord(
                id=attachment_id,
                thread_id=thread_id,
                name=name,
                size=size,
                path=str(path),
                kind=kind,
                media_type=media_type,
                created_at=datetime.now(UTC).isoformat(),
            )
            self._save_record(entry, record)
            return record
        except BaseException:
            for owned in (part, path, entry / "record.tmp", entry / "record.json"):
                if owned.is_file() and not owned.is_symlink():
                    owned.unlink(missing_ok=True)
            try:
                entry.rmdir()
            except OSError:
                pass
            raise

    def resolve(self, thread_id: str, attachment_id: str) -> AttachmentRecord:
        """发送消息前验证 ID 与线程归属，返回受控本机路径。"""
        return self._record(thread_id, attachment_id)

    async def bind(
        self, thread_id: str, attachment_ids: list[str], message_id: str
    ) -> list[AttachmentRecord]:
        """同一附件只能归属一条用户消息；同一消息重试时幂等。"""
        if not message_id:
            raise ValueError("消息标识不能为空")
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError("附件标识不能重复")
        async with self._lock:
            records = [self._record(thread_id, item) for item in attachment_ids]
            if any(record.bound_to not in {None, message_id} for record in records):
                raise RuntimeError("附件已绑定到其他消息")
            for record in records:
                if record.bound_to is None:
                    self._save_record(
                        self._entry_path(thread_id, record.id),
                        AttachmentRecord(**{**asdict(record), "bound_to": message_id}),
                    )
            return [self._record(thread_id, item) for item in attachment_ids]

    async def release_bindings(
        self, thread_id: str, attachment_ids: list[str], message_id: str
    ) -> None:
        """排队消息撤回时释放其附件；其他消息的绑定不受影响。"""
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError("附件标识不能重复")
        async with self._lock:
            records = [self._record(thread_id, item) for item in attachment_ids]
            if any(record.bound_to != message_id for record in records):
                raise RuntimeError("附件未绑定到这条消息")
            for record in records:
                self._save_record(
                    self._entry_path(thread_id, record.id),
                    AttachmentRecord(**{**asdict(record), "bound_to": None}),
                )

    def _remove_entry(self, record: AttachmentRecord) -> None:
        entry = self._entry_path(record.thread_id, record.id)
        self._check_owned(entry)
        path = Path(record.path)
        self._check_owned(path)
        expected = {record.name, "record.json"}
        if {item.name for item in entry.iterdir()} != expected:
            raise RuntimeError("附件目录包含未识别文件，已保留现场")
        path.unlink()
        (entry / "record.json").unlink()
        entry.rmdir()

    async def discard(self, thread_id: str, attachment_id: str) -> bool:
        """仅允许取消未绑定附件；不递归删除，也不跟随符号链接。"""
        async with self._lock:
            try:
                record = self._record(thread_id, attachment_id)
            except KeyError:
                return False
            if record.bound_to is not None:
                raise RuntimeError("已发送附件不能从待发送列表删除")
            self._remove_entry(record)
            return True

    async def remove_thread(self, thread_id: str) -> int:
        """删除会话后只移除该线程的有效附件记录，不递归清理未知文件。"""
        self._ensure_root()
        thread_path = self._thread_path(thread_id)
        self._check_owned(thread_path)
        if not thread_path.is_dir():
            return 0
        removed = 0
        async with self._lock:
            for entry in thread_path.iterdir():
                if (
                    not _IDENTIFIER.fullmatch(entry.name)
                    or entry.is_symlink()
                    or not entry.is_dir()
                ):
                    continue
                try:
                    record = self._record(thread_id, entry.name)
                    self._remove_entry(record)
                    removed += 1
                except (KeyError, ValueError, RuntimeError, PermissionError, OSError):
                    # 不认识的目录和文件不是本功能的清理对象。
                    continue
            try:
                thread_path.rmdir()
            except OSError:
                pass
        return removed

    async def cleanup_orphans(self, *, older_than_seconds: int = PENDING_TTL_SECONDS) -> int:
        """清理过期未绑定附件和残留临时文件，只认本应用的固定两级目录。"""
        if older_than_seconds < 0:
            raise ValueError("清理期限不能为负数")
        self._ensure_root()
        cutoff = time() - older_than_seconds
        removed = 0
        for thread_path in self.root.iterdir():
            if (
                not _THREAD_DIRECTORY.fullmatch(thread_path.name)
                or thread_path.is_symlink()
                or not thread_path.is_dir()
            ):
                continue
            self._check_owned(thread_path)
            for entry in thread_path.iterdir():
                if (
                    not _IDENTIFIER.fullmatch(entry.name)
                    or entry.is_symlink()
                    or not entry.is_dir()
                ):
                    continue
                self._check_owned(entry)
                if entry.stat().st_mtime > cutoff:
                    continue
                manifest = entry / "record.json"
                if manifest.is_file() and not manifest.is_symlink():
                    try:
                        raw = json.loads(manifest.read_text(encoding="utf-8"))
                        if self._thread_path(str(raw["thread_id"])) != thread_path:
                            continue
                        if raw.get("bound_to") is None:
                            removed += int(await self.discard(str(raw["thread_id"]), entry.name))
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        KeyError,
                        RuntimeError,
                        PermissionError,
                    ):
                        continue
                else:
                    # 中断于写入中的目录只移除预定临时名；额外文件会使 rmdir 失败并保留现场。
                    for temporary in (entry / "upload.part", entry / "record.tmp"):
                        if temporary.is_file() and not temporary.is_symlink():
                            temporary.unlink()
                    try:
                        entry.rmdir()
                        removed += 1
                    except OSError:
                        pass
            try:
                thread_path.rmdir()
            except OSError:
                pass
        return removed


__all__ = ["AttachmentRecord", "AttachmentStore", "AttachmentTooLargeError", "MAX_ATTACHMENT_BYTES"]
