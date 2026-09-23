"""基础检索先筛相关记录，再做有效性和结果数量限制。"""

from datetime import UTC, datetime
from pathlib import Path

from sayacode.memory.records import MemoryRecord, MemoryScope
from sayacode.memory.retrieval import MemoryRetriever


async def test_relevant_older_record_is_not_hidden_by_fifty_newer_unrelated_records(
    tmp_path: Path,
) -> None:
    user = MemoryScope("user", "local")
    project = MemoryScope("project", "repository")
    timestamp = datetime.now(UTC).isoformat()

    def record(index: int, subject: str) -> MemoryRecord:
        return MemoryRecord(
            id=f"memory-{index}",
            version=1,
            subject=subject,
            text=subject,
            scope=project,
            state="active",
            sources=(),
            applicability={},
            created_at=timestamp,
            updated_at=f"2026-09-23T00:{index:02}:00+00:00",
            source_order=str(index),
        )

    relevant = record(0, "解析器测试")

    class Repository:
        async def alist(self, scope, include_inactive=False):
            return [] if scope == user else [relevant, *(record(i, "无关记忆") for i in range(1, 60))]

    retriever = MemoryRetriever(Repository())  # type: ignore[arg-type]
    matches = await retriever.search(
        (user, project),
        tmp_path,
        limit=50,
        predicate=lambda item: "解析器" in item.subject,
    )
    assert [item.record.id for item in matches] == [relevant.id]
