"""已注册工作区目录；只保存产品元数据，不扫描用户磁盘。"""

from __future__ import annotations

import hashlib
import os
import string
from asyncio import to_thread
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.store.sqlite.aio import AsyncSqliteStore

_NAMESPACE = ("sayacode", "workspaces")
_BROWSE_LIMIT = 500


def _directory_roots() -> list[str]:
    """列出本机可进入的文件系统根，不借用浏览器上传目录权限。"""
    if os.name != "nt":
        return ["/"]
    # GetLogicalDrives 只读位掩码，不逐个探测可能离线的网络盘。
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{letter}:\\" for offset, letter in enumerate(string.ascii_uppercase) if mask & (1 << offset)]


def _browse_directory(path: str | None) -> dict[str, Any]:
    selected = Path(path).expanduser() if path else Path.home()
    if not selected.is_absolute():
        raise ValueError("请选择绝对目录路径")
    root = selected.resolve()
    if not root.is_dir():
        raise ValueError(f"目录不存在：{root}")

    directories: list[dict[str, str]] = []
    truncated = False
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if len(directories) == _BROWSE_LIMIT:
                    truncated = True
                    break
                directories.append({"name": entry.name, "path": str(root / entry.name)})
    except PermissionError as error:
        raise PermissionError(f"无权读取目录：{root}") from error
    except OSError as error:
        raise ValueError(f"无法读取目录：{root}（{error.strerror or error}）") from error
    directories.sort(key=lambda item: item["name"].casefold())
    parent = root.parent if root.parent != root else None
    return {
        "path": str(root),
        "parent": str(parent) if parent is not None else None,
        "roots": _directory_roots(),
        "directories": directories,
        "truncated": truncated,
    }


async def browse_directory(path: str | None = None) -> dict[str, Any]:
    """目录扫描可能触及慢盘，移到线程池避免阻塞 Agent 事件循环。"""
    return await to_thread(_browse_directory, path)


def workspace_id(path: Path) -> str:
    canonical = os.path.normcase(str(path.expanduser().resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


class WorkspaceRegistry:
    """显式登记的目录，可跨进程读取；会话历史仍归原生线程目录。"""

    def __init__(self, store: AsyncSqliteStore) -> None:
        self.store = store

    async def register(self, path: str | Path, name: str | None = None) -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"工作区目录不存在：{root}")
        selected_name = (name or root.name or str(root)).strip()
        if not selected_name:
            raise ValueError("工作区名称不能为空")
        identity = workspace_id(root)
        previous = await self.store.aget(_NAMESPACE, identity)
        existing = dict(previous.value) if previous is not None else {}
        now = datetime.now(UTC).isoformat()
        item = {
            **existing,
            "id": identity,
            "path": str(root),
            "name": selected_name if name is not None or not existing else existing["name"],
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
        await self.store.aput(_NAMESPACE, identity, item, index=False)
        return item

    async def get(self, identity: str) -> dict[str, Any]:
        item = await self.store.aget(_NAMESPACE, identity)
        if item is None:
            raise KeyError(f"未知工作区：{identity}")
        return dict(item.value)

    async def rename(self, identity: str, name: str) -> dict[str, Any]:
        selected = name.strip()
        if not selected:
            raise ValueError("工作区名称不能为空")
        item = await self.get(identity)
        item["name"] = selected
        item["updated_at"] = datetime.now(UTC).isoformat()
        await self.store.aput(_NAMESPACE, identity, item, index=False)
        return item

    async def list(self) -> list[dict[str, Any]]:
        offset = 0
        rows: list[dict[str, Any]] = []
        while True:
            page = await self.store.asearch(_NAMESPACE, limit=100, offset=offset)
            rows.extend(dict(item.value) for item in page)
            if len(page) < 100:
                break
            offset += len(page)
        return sorted(rows, key=lambda item: str(item.get("updated_at", "")), reverse=True)


__all__ = ["WorkspaceRegistry", "browse_directory", "workspace_id"]
