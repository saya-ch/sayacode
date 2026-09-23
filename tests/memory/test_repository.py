"""使用真实 SQLite Store 验证长期记忆的提交与恢复边界。"""

from __future__ import annotations

import asyncio
import subprocess
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path

import pytest
from langgraph.store.sqlite.aio import AsyncSqliteStore

from sayacode.memory import (
    MemoryChange,
    MemoryConflictError,
    MemoryRepository,
    MemoryScope,
    MemorySource,
)


def _remember_in_process(database: str, home: str, subject: str, order: int) -> None:
    async def save() -> None:
        async with AsyncSqliteStore.from_conn_string(database) as store:
            await store.setup()
            repository = MemoryRepository(store, home)
            await repository.aremember(
                MemoryScope("project", "shared-project"), subject, subject, source(order)
            )

    asyncio.run(save())


def source(number: int, *, kind: str = "user", ref: str | None = None) -> MemorySource:
    return MemorySource(
        ref=ref or f"turn-{number}",
        kind=kind,  # type: ignore[arg-type]
        order=f"2026-01-01T00:00:{number:02d}+00:00",
        thread_id="thread-1",
        checkpoint_id=f"checkpoint-{number}",
    )


@pytest.mark.asyncio
async def test_same_scope_survives_new_store_and_project_scopes_stay_separate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    database = tmp_path / "store.sqlite3"
    async with AsyncSqliteStore.from_conn_string(str(database)) as store:
        await store.setup()
        repository = MemoryRepository(store, home)
        user_a, project_scope_a = repository.scopes_for(project_a)
        user_b, project_scope_b = repository.scopes_for(project_b)
        assert user_a == user_b
        assert project_scope_a != project_scope_b
        await repository.aremember(user_a, "注释语言", "以后代码注释使用中文", source(1))
        await repository.aremember(project_scope_a, "包管理", "项目使用 uv", source(2))

    async with AsyncSqliteStore.from_conn_string(str(database)) as reopened:
        await reopened.setup()
        repository = MemoryRepository(reopened, home)
        user_a, project_scope_a = repository.scopes_for(project_a)
        _, project_scope_b = repository.scopes_for(project_b)
        assert [item.subject for item in await repository.alist(user_a)] == ["注释语言"]
        assert [item.subject for item in await repository.alist(project_scope_a)] == ["包管理"]
        assert await repository.alist(project_scope_b) == []
        state = await reopened.aget(user_a.namespace, "state")
        assert state is not None
        assert "messages" not in state.value


@pytest.mark.asyncio
async def test_git_worktree_shares_project_scope(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (root / "README.md").write_text("test", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"], check=True, capture_output=True)
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-b", "memory-test", str(worktree)],
        check=True,
        capture_output=True,
    )
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        nested = root / "src"
        nested.mkdir()
        assert repository.scopes_for(root)[1] == repository.scopes_for(worktree)[1]
        assert repository.scopes_for(nested)[1] == repository.scopes_for(root)[1]


@pytest.mark.asyncio
async def test_failed_single_put_preserves_previous_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "store.sqlite3"
    scope = MemoryScope("user", "owner-1")
    async with AsyncSqliteStore.from_conn_string(str(database)) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        original = await repository.aremember(scope, "注释语言", "中文", source(1))

        async def fail_put(*_args: object, **_kwargs: object) -> None:
            raise OSError("模拟写入失败")

        monkeypatch.setattr(store, "aput", fail_put)
        with pytest.raises(OSError, match="模拟写入失败"):
            await repository.acorrect(scope, original.id, "英文", source(2))
        assert (await repository.aget(scope, original.id)) == original

    async with AsyncSqliteStore.from_conn_string(str(database)) as reopened:
        await reopened.setup()
        repository = MemoryRepository(reopened, tmp_path / "home")
        assert (await repository.aget(scope, original.id)) == original


@pytest.mark.asyncio
async def test_late_job_cannot_override_new_user_correction(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        record = await repository.aremember(scope, "注释语言", "中文", source(1))
        job = await repository.aenqueue(scope, source(2, kind="assistant"))
        assert job.status == "pending"
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        snapshot = await repository.aread(scope)
        await repository.acorrect(scope, record.id, "英文", source(3))
        accepted = await repository.acommit(
            scope,
            claimed,
            snapshot,
            [MemoryChange(action="update", record_id=record.id, text="中文")],
        )
        assert not accepted
        current = await repository.aget(scope, record.id)
        assert current is not None and current.text == "英文"
        assert (await repository.aread(scope)).pending[claimed.id].status == "pending"


@pytest.mark.asyncio
async def test_invalid_second_change_does_not_partially_commit_first(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        record = await repository.aremember(scope, "测试", "运行 pytest", source(1))
        await repository.aenqueue(scope, source(2, kind="tool"))
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        snapshot = await repository.aread(scope)
        with pytest.raises(ValueError, match="不能为空"):
            await repository.acommit(
                scope,
                claimed,
                snapshot,
                [
                    MemoryChange(action="update", record_id=record.id, text="运行 uv run pytest"),
                    MemoryChange(action="insert", subject="", text="无效"),
                ],
            )
        assert await repository.aget(scope, record.id) == record
        assert (await repository.aread(scope)).pending[claimed.id].status == "claimed"


@pytest.mark.asyncio
async def test_model_cannot_commit_unlisted_evidence_reference(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        source_ref = MemorySource(
            ref="turn-1",
            kind="user",
            order="2026-01-01T00:00:01+00:00",
            evidence_refs=("user-message-1",),
        )
        await repository.aenqueue(scope, source_ref)
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        snapshot = await repository.aread(scope)
        with pytest.raises(ValueError, match="未授权"):
            await repository.acommit(
                scope,
                claimed,
                snapshot,
                [
                    MemoryChange(
                        action="insert",
                        subject="偏好",
                        text="从未说过的偏好",
                        evidence_refs=("forged-message",),
                    )
                ],
            )
        assert await repository.alist(scope) == []


@pytest.mark.asyncio
async def test_forget_removes_text_and_cancels_inflight_job(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        record = await repository.aremember(scope, "默认语言", "使用中文", source(1))
        await repository.aenqueue(scope, source(2, kind="assistant"))
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        snapshot = await repository.aread(scope)
        await repository.aforget(scope, record.id, source(3))
        assert await repository.aget(scope, record.id) is None
        assert await repository.alist(scope) == []
        assert not await repository.acommit(
            scope,
            claimed,
            snapshot,
            [MemoryChange(action="insert", subject="默认语言", text="使用中文")],
        )
        state = await repository.aread(scope)
        assert claimed.id not in state.pending
        assert all("使用中文" not in str(mark) for mark in state.tombstones.values())
        stale = await repository.aenqueue(scope, source(2, kind="assistant", ref="other-old"))
        assert stale.status == "completed"
        with pytest.raises(MemoryConflictError):
            await repository.aremember(scope, "默认语言", "使用中文", source(2, ref="old"))
        restored = await repository.aremember(scope, "默认语言", "重新偏好中文", source(4))
        assert restored.text == "重新偏好中文"


@pytest.mark.asyncio
async def test_claim_expires_and_parallel_process_like_connections_do_not_lose_updates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "store.sqlite3"
    async with (
        AsyncSqliteStore.from_conn_string(str(database)) as first_store,
        AsyncSqliteStore.from_conn_string(str(database)) as second_store,
    ):
        await first_store.setup()
        await second_store.setup()
        first = MemoryRepository(first_store, tmp_path / "home")
        second = MemoryRepository(second_store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        await asyncio.gather(
            first.aremember(scope, "编译", "运行 uv build", source(1)),
            second.aremember(scope, "测试", "运行 pytest", source(2)),
        )
        assert {item.subject for item in await first.alist(scope)} == {"编译", "测试"}
        await first.aenqueue(scope, source(3, kind="assistant"))
        claim = await first.aclaim(scope, "worker-1", lease_seconds=1)
        assert claim is not None
        await second.aenqueue(scope, source(4, kind="assistant"))
        assert await second.aclaim(scope, "worker-2") is None
        await asyncio.sleep(1.1)
        recovered = await second.aclaim(scope, "worker-2")
        assert recovered is not None and recovered.id == claim.id
        assert recovered.lease_token != claim.lease_token


def test_two_cli_processes_do_not_lose_updates(tmp_path: Path) -> None:
    """两个进程各有 SQLite 连接时，提交仍按同一作用域串行。"""
    database = str(tmp_path / "store.sqlite3")
    home = str(tmp_path / "home")

    async def setup() -> None:
        async with AsyncSqliteStore.from_conn_string(database) as store:
            await store.setup()

    asyncio.run(setup())
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as executor:
        first = executor.submit(_remember_in_process, database, home, "编译", 1)
        second = executor.submit(_remember_in_process, database, home, "测试", 2)
        first.result(timeout=20)
        second.result(timeout=20)

    async def inspect() -> set[str]:
        async with AsyncSqliteStore.from_conn_string(database) as store:
            await store.setup()
            repository = MemoryRepository(store, home)
            return {
                record.subject
                for record in await repository.alist(MemoryScope("project", "shared-project"))
            }

    assert asyncio.run(inspect()) == {"编译", "测试"}


@pytest.mark.asyncio
async def test_user_scope_claims_only_the_current_project_after_restart(tmp_path: Path) -> None:
    """用户级队列共享 Store，但项目 B 不领取或等待项目 A 的来源。"""
    database = str(tmp_path / "store.sqlite3")
    home = tmp_path / "home"
    scope = MemoryScope("user", "owner-1")
    from_a = MemorySource(
        ref="project-a-turn",
        kind="user",
        order="2026-01-01T00:00:01+00:00",
        project_id="project-a",
        user_message_id="human-a",
    )
    from_b = MemorySource(
        ref="project-b-turn",
        kind="user",
        order="2026-01-01T00:00:02+00:00",
        project_id="project-b",
        user_message_id="human-b",
    )
    async with AsyncSqliteStore.from_conn_string(database) as first_store:
        await first_store.setup()
        first = MemoryRepository(first_store, home)
        await first.aenqueue(scope, from_a)
        await first.aenqueue(scope, from_b)
        a_job = await first.aclaim(scope, "cli-a", project_id="project-a")
        assert a_job is not None and a_job.source.user_message_id == "human-a"

    async with AsyncSqliteStore.from_conn_string(database) as reopened:
        await reopened.setup()
        second = MemoryRepository(reopened, home)
        b_job = await second.aclaim(scope, "cli-b", project_id="project-b")
        assert b_job is not None and b_job.source.ref == from_b.ref
        assert b_job.source.user_message_id == "human-b"
        assert await second.aclaim(scope, "cli-a-duplicate", project_id="project-a") is None
        assert await second.aclaim(scope, "unknown", project_id="project-c") is None


@pytest.mark.asyncio
async def test_headless_claims_only_its_exact_source(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        first = replace(source(1), project_id="project-a")
        second = replace(source(2), project_id="project-a")
        await repository.aenqueue(scope, first)
        await repository.aenqueue(scope, second)
        claimed = await repository.aclaim(
            scope, "headless", project_id="project-a", source_ref=second.ref
        )
        assert claimed is not None and claimed.source.ref == second.ref
        other = await repository.aclaim(
            scope, "interactive", project_id="project-a", source_ref=first.ref
        )
        assert other is not None and other.source.ref == first.ref
        assert await repository.aclaim(scope, "missing", source_ref="no-such-turn") is None


@pytest.mark.asyncio
async def test_expired_or_due_for_review_is_not_active(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        record = await repository.aremember(scope, "构建方式", "运行 uv build", source(1))
        assert record.review_after is not None
        assert await repository.alist(scope)
        item = await store.aget(scope.namespace, "state")
        assert item is not None
        document = item.value
        document["records"][record.id]["review_after"] = "2020-01-01T00:00:00+00:00"
        await store.aput(scope.namespace, "state", document, index=False)
        assert await repository.alist(scope) == []
        assert [entry.id for entry in await repository.alist(scope, include_inactive=True)] == [
            record.id
        ]
        document["records"][record.id]["review_after"] = None
        document["records"][record.id]["expires_at"] = "无效时间"
        await store.aput(scope.namespace, "state", document, index=False)
        assert await repository.alist(scope) == []


@pytest.mark.asyncio
async def test_user_confirmation_renews_due_or_expired_memory(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        record = await repository.aremember(scope, "构建方式", "运行 uv build", source(1))
        item = await store.aget(scope.namespace, "state")
        assert item is not None
        document = item.value
        document["records"][record.id]["review_after"] = "2020-01-01T00:00:00+00:00"
        document["records"][record.id]["expires_at"] = "2020-01-01T00:00:00+00:00"
        await store.aput(scope.namespace, "state", document, index=False)
        assert await repository.alist(scope) == []
        renewed = await repository.aconfirm(scope, record.id, source(2))
        assert renewed.state == "active"
        assert renewed.review_after is not None
        assert renewed.review_after > source(2).order
        assert renewed.expires_at is None
        assert [item.id for item in await repository.alist(scope)] == [record.id]


@pytest.mark.asyncio
async def test_file_refresh_keeps_explicit_expiration(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        record = await repository.aremember(scope, "构建方式", "运行 uv build", source(1))
        item = await store.aget(scope.namespace, "state")
        assert item is not None
        document = item.value
        document["records"][record.id]["applicability"] = {
            "path": "pyproject.toml",
            "sha256": "a" * 64,
        }
        document["records"][record.id]["review_after"] = "2020-01-01T00:00:00+00:00"
        document["records"][record.id]["expires_at"] = "2099-01-01T00:00:00+00:00"
        await store.aput(scope.namespace, "state", document, index=False)
        refreshed = await repository.arefresh_from_file(scope, record.id, source(2, kind="tool"))
        assert refreshed.state == "active"
        assert refreshed.review_after is not None
        assert refreshed.expires_at == "2099-01-01T00:00:00+00:00"
        assert [item.id for item in await repository.alist(scope)] == [record.id]
        item = await store.aget(scope.namespace, "state")
        assert item is not None
        document = item.value
        document["records"][record.id]["expires_at"] = "2020-01-01T00:00:00+00:00"
        await store.aput(scope.namespace, "state", document, index=False)
        with pytest.raises(MemoryConflictError, match="显式过期"):
            await repository.arefresh_from_file(scope, record.id, source(3, kind="tool"))


@pytest.mark.asyncio
async def test_pin_and_unpin_are_explicit_and_do_not_bypass_validity(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        record = await repository.aremember(scope, "测试方式", "运行 pytest", source(1))
        pinned = await repository.apin(scope, record.id, source(2))
        assert pinned.pinned and pinned.version == record.version + 1
        assert (await repository.apin(scope, record.id, source(2))) == pinned
        with pytest.raises(MemoryConflictError):
            await repository.aunpin(scope, record.id, source(1, ref="old-unpin"))
        item = await store.aget(scope.namespace, "state")
        assert item is not None
        document = item.value
        document["records"][record.id]["expires_at"] = "2020-01-01T00:00:00+00:00"
        await store.aput(scope.namespace, "state", document, index=False)
        assert await repository.alist(scope) == []
        unpinned = await repository.aunpin(scope, record.id, source(3))
        assert not unpinned.pinned
        assert await repository.alist(scope) == []
        await repository.aforget(scope, record.id, source(4))
        assert await repository.aget(scope, record.id) is None


@pytest.mark.asyncio
async def test_noop_unpin_advances_order_and_blocks_older_pin(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        record = await repository.aremember(scope, "语言", "使用中文", source(1))
        unchanged = await repository.aunpin(scope, record.id, source(3))
        assert not unchanged.pinned
        assert unchanged.version == record.version + 1
        with pytest.raises(MemoryConflictError):
            await repository.apin(scope, record.id, source(2))


@pytest.mark.asyncio
async def test_repeat_forget_extends_barrier_to_newer_user_source(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        record = await repository.aremember(scope, "语言", "使用中文", source(1))
        await repository.aforget(scope, record.id, source(2))
        await repository.aforget(scope, record.id, source(4))
        with pytest.raises(MemoryConflictError):
            await repository.aremember(scope, "语言", "旧要求", source(3))
        restored = await repository.aremember(scope, "语言", "重新记住", source(5))
        assert restored.text == "重新记住"


@pytest.mark.asyncio
async def test_new_verified_project_change_renews_review_date(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        record = await repository.aremember(scope, "测试", "使用 pytest", source(1))
        item = await store.aget(scope.namespace, "state")
        assert item is not None
        document = item.value
        document["records"][record.id]["review_after"] = "2020-01-01T00:00:00+00:00"
        await store.aput(scope.namespace, "state", document, index=False)
        assert await repository.alist(scope) == []
        await repository.aenqueue(scope, source(2, kind="tool"))
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        snapshot = await repository.aread(scope)
        assert await repository.acommit(
            scope,
            claimed,
            snapshot,
            [MemoryChange(action="update", record_id=record.id, text="使用 uv run pytest")],
        )
        updated = await repository.aget(scope, record.id)
        assert updated is not None and updated.review_after is not None
        assert [entry.id for entry in await repository.alist(scope)] == [record.id]


@pytest.mark.asyncio
async def test_released_job_keeps_original_source_and_does_not_store_secret_error(
    tmp_path: Path,
) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        original = source(1, kind="assistant")
        await repository.aenqueue(scope, original)
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        assert not await repository.arelease(scope, replace(claimed, lease_token=""))
        tampered = replace(claimed, source=source(2, kind="assistant"))
        assert await repository.arelease(scope, tampered, error="api_key=secret-value")
        pending = (await repository.aread(scope)).pending[claimed.id]
        assert pending.source == original
        assert pending.last_error == "failed"
        assert "secret-value" not in str((await store.aget(scope.namespace, "state")).value)


@pytest.mark.asyncio
async def test_committed_source_is_idempotent_but_cannot_change_identity(tmp_path: Path) -> None:
    database = str(tmp_path / "store.sqlite3")
    async with AsyncSqliteStore.from_conn_string(database) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        origin = source(1, kind="assistant")
        await repository.aenqueue(scope, origin)
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        assert not await repository.acommit(
            scope,
            replace(claimed, source=replace(origin, project_id="forged")),
            await repository.aread(scope),
            [],
        )
        assert await repository.acommit(scope, claimed, await repository.aread(scope), [])
        assert (await repository.aenqueue(scope, origin)).status == "completed"
        with pytest.raises(MemoryConflictError, match="不一致"):
            await repository.aenqueue(scope, replace(origin, user_message_id="different"))

    async with AsyncSqliteStore.from_conn_string(database) as reopened:
        await reopened.setup()
        repository = MemoryRepository(reopened, tmp_path / "home")
        assert (await repository.aenqueue(scope, origin)).status == "completed"
        assert await repository.aclaim(scope, "new-cli") is None


@pytest.mark.asyncio
async def test_expired_claim_recovers_in_new_process_session(tmp_path: Path) -> None:
    database = str(tmp_path / "store.sqlite3")
    origin = source(1, kind="assistant")
    scope = MemoryScope("project", "project-1")
    async with AsyncSqliteStore.from_conn_string(database) as first_store:
        await first_store.setup()
        repository = MemoryRepository(first_store, tmp_path / "home")
        await repository.aenqueue(scope, origin)
        first_claim = await repository.aclaim(scope, "first-cli", lease_seconds=1)
        assert first_claim is not None

    await asyncio.sleep(1.1)
    async with AsyncSqliteStore.from_conn_string(database) as reopened:
        await reopened.setup()
        repository = MemoryRepository(reopened, tmp_path / "home")
        resumed = await repository.aclaim(scope, "new-cli")
        assert resumed is not None
        assert resumed.source == origin
        assert resumed.lease_token != first_claim.lease_token


@pytest.mark.asyncio
async def test_invalidated_source_cannot_be_relearned_after_reopen(tmp_path: Path) -> None:
    database = str(tmp_path / "store.sqlite3")
    scope = MemoryScope("user", "owner-1")
    origin = source(1)
    async with AsyncSqliteStore.from_conn_string(database) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        await repository.aenqueue(scope, origin)
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        assert not await repository.ainvalidate(
            scope, replace(claimed, lease_token=""), "cancelled"
        )
        assert await repository.ainvalidate(scope, claimed, "cancelled")
        assert await repository.aclaim(scope, "worker") is None
        assert (await repository.aread(scope)).processed[origin.ref]["status"] == "cancelled"

    async with AsyncSqliteStore.from_conn_string(database) as reopened:
        await reopened.setup()
        repository = MemoryRepository(reopened, tmp_path / "home")
        assert (await repository.aenqueue(scope, origin)).status == "completed"
        assert await repository.aclaim(scope, "new-cli") is None


@pytest.mark.asyncio
async def test_revoke_cancels_claimed_and_pending_sources_before_old_commit(tmp_path: Path) -> None:
    database = str(tmp_path / "store.sqlite3")
    async with (
        AsyncSqliteStore.from_conn_string(database) as first_store,
        AsyncSqliteStore.from_conn_string(database) as second_store,
    ):
        await first_store.setup()
        await second_store.setup()
        first = MemoryRepository(first_store, tmp_path / "home")
        second = MemoryRepository(second_store, tmp_path / "home")
        scope = MemoryScope("project", "project-1")
        in_flight = source(1)
        not_started = source(2)
        await first.aenqueue(scope, in_flight)
        await first.aenqueue(scope, not_started)
        claimed_event = asyncio.Event()
        continue_event = asyncio.Event()

        async def old_worker() -> bool:
            claimed = await first.aclaim(scope, "old-worker", source_ref=in_flight.ref)
            assert claimed is not None
            snapshot = await first.aread(scope)
            claimed_event.set()
            await continue_event.wait()
            return await first.acommit(
                scope,
                claimed,
                snapshot,
                [MemoryChange(action="insert", subject="旧事实", text="不应落库")],
            )

        task = asyncio.create_task(old_worker())
        await claimed_event.wait()
        assert await second.arevoke(scope, {in_flight.ref, not_started.ref, "missing"}) == 2
        continue_event.set()
        assert not await task
        snapshot = await second.aread(scope)
        assert snapshot.pending == {}
        assert snapshot.records == {}
        assert snapshot.processed[in_flight.ref]["status"] == "cancelled"
        assert snapshot.processed[not_started.ref]["status"] == "cancelled"
        assert (await first.aenqueue(scope, in_flight)).status == "completed"

        finished = source(3)
        await first.aenqueue(scope, finished)
        claimed = await first.aclaim(scope, "new-worker")
        assert claimed is not None
        assert await first.acommit(scope, claimed, await first.aread(scope), [])
        assert await second.arevoke(scope, {finished.ref}) == 0
        assert (await second.aread(scope)).processed[finished.ref]["status"] == "completed"


@pytest.mark.asyncio
async def test_obsolete_job_finishes_instead_of_retrying_forever(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        stale = source(1, kind="assistant")
        await repository.aenqueue(scope, stale)
        claimed = await repository.aclaim(scope, "worker")
        assert claimed is not None
        snapshot = await repository.aread(scope)
        await repository.aremember(scope, "语言", "以后用英文", source(2))
        change = MemoryChange(action="insert", subject="语言", text="以后用中文")
        assert not await repository.acommit(scope, claimed, snapshot, [change])
        renewed = await repository.aclaim(scope, "worker")
        assert renewed is not None
        assert not await repository.acommit(
            scope, renewed, await repository.aread(scope), [change]
        )
        state = await repository.aread(scope)
        assert renewed.id not in state.pending
        assert state.processed[stale.ref]["status"] == "superseded"
        assert await repository.aclaim(scope, "worker") is None


@pytest.mark.asyncio
async def test_candidates_require_explicit_confirmation(tmp_path: Path) -> None:
    async with AsyncSqliteStore.from_conn_string(str(tmp_path / "store.sqlite3")) as store:
        await store.setup()
        repository = MemoryRepository(store, tmp_path / "home")
        scope = MemoryScope("user", "owner-1")
        record = await repository.aremember(scope, "可能偏好", "也许想要短回答", source(1), state="candidate")
        record = await repository.aremember(
            scope, "可能偏好", "似乎想要短回答", source(2), state="candidate"
        )
        assert record.confirmed_at is None
        assert await repository.alist(scope) == []
        confirmed = await repository.aconfirm(scope, record.id, source(3))
        assert confirmed.state == "active"
        assert confirmed.confirmed_at is not None
        assert [item.id for item in await repository.alist(scope)] == [record.id]
