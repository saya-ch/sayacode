"""从权威 Store 选择当前任务需要的有效记忆。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .records import MemoryRecord, MemoryScope
from .repository import MemoryRepository


@dataclass(frozen=True, slots=True)
class MemoryMatch:
    """一条记忆与当前工作树的核验结果。"""

    record: MemoryRecord
    current: bool
    reason: str


def _not_after_now(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        return True
    return parsed <= datetime.now(UTC)


def _workspace_file(workspace: Path, relative_path: str) -> Path | None:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        return None
    root = workspace.expanduser().resolve()
    target = (root / path).resolve()
    return target if target.is_relative_to(root) and target.is_file() else None


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MemoryRetriever:
    """先核对有效性，再用范围和任务文字选择少量记忆。"""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def _classify(self, record: MemoryRecord, workspace: Path) -> MemoryMatch:
        if record.state != "active":
            return MemoryMatch(record, False, f"状态为 {record.state}")
        if _not_after_now(record.expires_at):
            return MemoryMatch(record, False, "明确有效期已过")
        if _not_after_now(record.review_after):
            return MemoryMatch(record, False, "需要重新确认")
        conditions = record.applicability
        if record.scope.kind == "project" and "path" in conditions:
            target = _workspace_file(workspace, conditions["path"])
            if target is None:
                return MemoryMatch(record, False, "依据文件在当前工作树不可用")
            expected = conditions.get("sha256")
            if expected and await asyncio.to_thread(_file_digest, target) != expected:
                return MemoryMatch(record, False, "当前代码与保存时的依据不同")
        return MemoryMatch(record, True, "当前范围内有效")

    async def search(
        self,
        scopes: tuple[MemoryScope, MemoryScope],
        workspace: Path,
        *,
        query: str = "",
        limit: int = 10,
        include_historical: bool = False,
        predicate: Callable[[MemoryRecord], bool] | None = None,
    ) -> list[MemoryMatch]:
        """返回实际命中的记录，历史线索只在显式请求时返回。"""
        if not 1 <= limit <= 50:
            raise ValueError("记忆结果数量须在 1 至 50 之间")
        records_by_scope = await asyncio.gather(
            *(self.repository.alist(scope, include_inactive=include_historical) for scope in scopes)
        )
        matches: list[MemoryMatch] = []
        asked = query.casefold().strip()
        for records in records_by_scope:
            for record in records:
                if asked and asked not in f"{record.subject} {record.text}".casefold():
                    continue
                if predicate is not None and not predicate(record):
                    continue
                result = await self._classify(record, workspace)
                if result.current or include_historical:
                    matches.append(result)
        return sorted(
            matches,
            key=lambda item: (item.current, item.record.pinned, item.record.updated_at),
            reverse=True,
        )[:limit]


__all__ = ["MemoryMatch", "MemoryRetriever"]
