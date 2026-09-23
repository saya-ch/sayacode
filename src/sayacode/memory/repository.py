"""以 LangGraph Store 的单作用域文档保存长期记忆。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from filelock import AsyncFileLock, FileLock
from langgraph.store.sqlite.aio import AsyncSqliteStore

from .records import (
    MemoryChange,
    MemoryConflictError,
    MemoryJob,
    MemoryRecord,
    MemoryScope,
    MemorySnapshot,
    MemorySource,
    MemoryState,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _past(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed.tzinfo is None or parsed <= datetime.now(timezone.utc)


def _subject_key(subject: str) -> str:
    return " ".join(subject.casefold().split())


def _subject_digest(subject: str) -> str:
    return hashlib.sha256(_subject_key(subject).encode("utf-8")).hexdigest()


def _source_fingerprint(source: MemorySource) -> str:
    encoded = json.dumps(asdict(source), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_error_code(error: str) -> str:
    # 异常正文可能包含凭据或用户内容，只保存约定的诊断代码。
    allowed = {"cancelled", "source_missing", "model_changed", "extract_failed", "commit_failed"}
    return error if error in allowed else "failed" if error else ""


def _next_review_at(scope: MemoryScope, state: str) -> str | None:
    # 项目事实会随代码演变；明确用户偏好不设置日历复核期。
    if scope.kind != "project" or state != "active":
        return None
    return (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()


def _document() -> dict[str, Any]:
    return {
        "schema": 1,
        "revision": 0,
        "control_epoch": 0,
        "records": {},
        "pending": {},
        "processed": {},
        "tombstones": {},
    }


class MemoryRepository:
    """Store 的单一写入口；跨进程锁只覆盖读取、校验和单次提交。"""

    def __init__(self, store: AsyncSqliteStore, home: str | Path) -> None:
        self.store = store
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self._locks = self.home / "memory-locks"
        self._locks.mkdir(exist_ok=True)

    def scopes_for(
        self, workspace: str | Path, parent_workspace: str | Path | None = None
    ) -> tuple[MemoryScope, MemoryScope]:
        """同一安装共享用户身份；Git worktree 共享 git-common-dir 身份。"""
        owner_path = self.home / "memory-owner-id"
        with FileLock(str(self.home / "memory-owner-id.lock"), timeout=10):
            if not owner_path.exists():
                identity = uuid.uuid4().hex
                temporary = self.home / f".{owner_path.name}.{uuid.uuid4().hex}.tmp"
                try:
                    temporary.write_text(identity, encoding="ascii")
                    os.replace(temporary, owner_path)
                finally:
                    temporary.unlink(missing_ok=True)
            owner_id = owner_path.read_text(encoding="ascii").strip()
        if not owner_id:
            raise ValueError("本地记忆身份文件为空")
        project_path = Path(parent_workspace or workspace).expanduser().resolve()
        common_dir = self._git_common_dir(project_path)
        canonical = common_dir or project_path
        path_key = os.path.normcase(str(canonical))
        project_id = hashlib.sha256(path_key.encode("utf-8")).hexdigest()
        return MemoryScope("user", owner_id), MemoryScope("project", project_id)

    @staticmethod
    def _git_common_dir(workspace: Path) -> Path | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode or not result.stdout.strip():
            return None
        location = Path(result.stdout.strip())
        return (location if location.is_absolute() else workspace / location).resolve()

    def _lock(self, scope: MemoryScope) -> AsyncFileLock:
        key = hashlib.sha256("/".join(scope.namespace).encode("utf-8")).hexdigest()
        return AsyncFileLock(str(self._locks / f"{key}.lock"), timeout=15)

    async def _read(self, scope: MemoryScope) -> dict[str, Any]:
        item = await self.store.aget(scope.namespace, "state")
        if item is None:
            return _document()
        raw = item.value
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            raise ValueError("记忆 Store 文档格式无效")
        # Store 返回的是 JSON 值；只在本次短临界区内修改新的容器。
        return {
            "schema": 1,
            "revision": int(raw["revision"]),
            "control_epoch": int(raw["control_epoch"]),
            "records": dict(raw["records"]),
            "pending": dict(raw["pending"]),
            "processed": dict(raw["processed"]),
            "tombstones": dict(raw["tombstones"]),
        }

    async def _write(self, scope: MemoryScope, state: dict[str, Any]) -> None:
        state["revision"] += 1
        await self.store.aput(scope.namespace, "state", state, index=False)

    @staticmethod
    def _mark_processed(state: dict[str, Any], job: MemoryJob, status: str) -> None:
        state["processed"][job.source.ref] = {
            "job_id": job.id,
            "status": status,
            "source_sha256": _source_fingerprint(job.source),
            "at": _now(),
        }

    @staticmethod
    def _snapshot(scope: MemoryScope, state: Mapping[str, Any]) -> MemorySnapshot:
        return MemorySnapshot(
            scope=scope,
            revision=int(state["revision"]),
            control_epoch=int(state["control_epoch"]),
            records={
                key: MemoryRecord.from_dict(value, scope)
                for key, value in state["records"].items()
            },
            pending={
                key: MemoryJob.from_dict(value) for key, value in state["pending"].items()
            },
            processed=dict(state["processed"]),
            tombstones=dict(state["tombstones"]),
        )

    async def aread(self, scope: MemoryScope) -> MemorySnapshot:
        return self._snapshot(scope, await self._read(scope))

    async def alist(
        self, scope: MemoryScope, *, include_inactive: bool = False
    ) -> list[MemoryRecord]:
        snapshot = await self.aread(scope)
        records = list(snapshot.records.values())
        if not include_inactive:
            records = [
                record
                for record in records
                if record.state == "active"
                and not _past(record.expires_at)
                and not _past(record.review_after)
            ]
        return sorted(records, key=lambda record: (record.updated_at, record.id), reverse=True)

    async def aget(self, scope: MemoryScope, record_id: str) -> MemoryRecord | None:
        return (await self.aread(scope)).records.get(record_id)

    @staticmethod
    def _record_id(scope: MemoryScope, source_ref: str, key: str) -> str:
        material = "\x00".join((*scope.namespace, source_ref, key))
        return "mem-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _check_subject_barrier(
        state: dict[str, Any], subject: str, source: MemorySource, *, reaffirmed: bool
    ) -> None:
        digest = _subject_digest(subject)
        barrier = state["tombstones"].get(digest)
        if barrier is None:
            return
        if not reaffirmed or source.kind != "user" or source.order <= barrier["source_order"]:
            raise MemoryConflictError("这项记忆已遗忘；旧来源不能重新建立它")
        del state["tombstones"][digest]

    @staticmethod
    def _new_record(
        scope: MemoryScope,
        record_id: str,
        subject: str,
        text: str,
        source: MemorySource,
        state: str,
        applicability: Mapping[str, str] | None = None,
        *,
        confirmed: bool = False,
    ) -> MemoryRecord:
        if not subject.strip() or not text.strip():
            raise ValueError("记忆主题和正文不能为空")
        if state not in ("active", "candidate", "needs_verification", "replaced"):
            raise ValueError("未知的记忆状态")
        now = _now()
        return MemoryRecord(
            id=record_id,
            version=1,
            subject=subject.strip(),
            text=text.strip(),
            scope=scope,
            state=cast(MemoryState, state),
            sources=(source,),
            applicability=dict(applicability or {}),
            created_at=now,
            updated_at=now,
            source_order=source.order,
            confirmed_at=now if confirmed and state == "active" else None,
            review_after=_next_review_at(scope, state),
        )

    async def aremember(
        self,
        scope: MemoryScope,
        subject: str,
        text: str,
        source: MemorySource,
        *,
        state: str = "active",
    ) -> MemoryRecord:
        """用户明确记住或修订一个主题；相同来源重复执行保持幂等。"""
        if source.kind != "user":
            raise ValueError("直接记忆修改需要用户来源")
        if not subject.strip() or not text.strip():
            raise ValueError("记忆主题和正文不能为空")
        if state not in ("active", "candidate", "needs_verification"):
            raise ValueError("未知的记忆状态")
        async with self._lock(scope):
            data = await self._read(scope)
            self._check_subject_barrier(data, subject, source, reaffirmed=True)
            existing = next(
                (
                    MemoryRecord.from_dict(value, scope)
                    for value in data["records"].values()
                    if value["state"] != "replaced"
                    and _subject_key(str(value["subject"])) == _subject_key(subject)
                ),
                None,
            )
            if existing is not None:
                if any(previous.ref == source.ref for previous in existing.sources):
                    if existing.text == text.strip() and existing.state == state:
                        return existing
                    raise MemoryConflictError("同一来源引用的记忆内容不一致")
                if source.order <= existing.source_order:
                    raise MemoryConflictError("旧用户来源不能覆盖较新的偏好")
                updated = replace(
                    existing,
                    version=existing.version + 1,
                    text=text.strip(),
                    state=cast(MemoryState, state),
                    sources=(*existing.sources, source),
                    updated_at=_now(),
                    source_order=source.order,
                    confirmed_at=_now() if state == "active" else existing.confirmed_at,
                    review_after=_next_review_at(scope, state),
                    expires_at=None if state == "active" else existing.expires_at,
                )
            else:
                record_id = self._record_id(scope, source.ref, _subject_key(subject))
                updated = self._new_record(
                    scope, record_id, subject, text, source, state, confirmed=True
                )
            data["records"][updated.id] = updated.to_dict()
            data["control_epoch"] += 1
            await self._write(scope, data)
            return updated

    async def acorrect(
        self, scope: MemoryScope, record_id: str, text: str, source: MemorySource
    ) -> MemoryRecord:
        if source.kind != "user" or not text.strip():
            raise ValueError("纠正需要用户来源和非空正文")
        async with self._lock(scope):
            data = await self._read(scope)
            raw = data["records"].get(record_id)
            if raw is None:
                raise KeyError(record_id)
            record = MemoryRecord.from_dict(raw, scope)
            if record.state == "replaced":
                raise MemoryConflictError("已替代的记忆不能直接纠正")
            if any(previous.ref == source.ref for previous in record.sources):
                if record.text == text.strip() and record.state == "active":
                    return record
                raise MemoryConflictError("同一来源引用的纠正内容不一致")
            if source.order <= record.source_order:
                raise MemoryConflictError("旧用户来源不能覆盖较新的记忆")
            updated = replace(
                record,
                version=record.version + 1,
                text=text.strip(),
                state="active",
                sources=(*record.sources, source),
                updated_at=_now(),
                source_order=source.order,
                confirmed_at=_now(),
                review_after=_next_review_at(scope, "active"),
                expires_at=None,
            )
            data["records"][record_id] = updated.to_dict()
            data["control_epoch"] += 1
            await self._write(scope, data)
            return updated

    async def aconfirm(
        self, scope: MemoryScope, record_id: str, source: MemorySource
    ) -> MemoryRecord:
        """用户核实候选或待核验记录；读取和模型复述不构成确认。"""
        if source.kind != "user":
            raise ValueError("确认记忆需要用户来源")
        async with self._lock(scope):
            data = await self._read(scope)
            raw = data["records"].get(record_id)
            if raw is None:
                raise KeyError(record_id)
            record = MemoryRecord.from_dict(raw, scope)
            if record.state == "replaced":
                raise MemoryConflictError("已替代的记忆不能重新确认")
            if any(previous.ref == source.ref for previous in record.sources):
                if record.state == "active":
                    return record
                raise MemoryConflictError("同一来源引用不能执行不同的记忆操作")
            if source.order <= record.source_order:
                raise MemoryConflictError("旧用户来源不能确认较新的记忆")
            updated = replace(
                record,
                version=record.version + 1,
                state="active",
                sources=(*record.sources, source),
                updated_at=_now(),
                source_order=source.order,
                confirmed_at=_now(),
                review_after=_next_review_at(scope, "active"),
                expires_at=None,
            )
            data["records"][record_id] = updated.to_dict()
            data["control_epoch"] += 1
            await self._write(scope, data)
            return updated

    async def apin(
        self, scope: MemoryScope, record_id: str, source: MemorySource
    ) -> MemoryRecord:
        """用户固定一条记忆；固定不改变事实的有效期和核验结果。"""
        return await self._set_pin(scope, record_id, source, pinned=True)

    async def aunpin(
        self, scope: MemoryScope, record_id: str, source: MemorySource
    ) -> MemoryRecord:
        """用户取消固定，不影响正文和已有来源。"""
        return await self._set_pin(scope, record_id, source, pinned=False)

    async def arefresh_from_file(
        self, scope: MemoryScope, record_id: str, source: MemorySource
    ) -> MemoryRecord:
        """外部已核对文件摘要时续延项目复核期，不改写显式有效期。"""
        if scope.kind != "project" or source.kind != "tool":
            raise ValueError("文件复核需要项目作用域和工具依据")
        async with self._lock(scope):
            data = await self._read(scope)
            raw = data["records"].get(record_id)
            if raw is None:
                raise KeyError(record_id)
            record = MemoryRecord.from_dict(raw, scope)
            if record.state == "replaced" or _past(record.expires_at):
                raise MemoryConflictError("已替代或显式过期的记忆不能通过文件复核恢复")
            if not record.applicability.get("path") or not record.applicability.get("sha256"):
                raise ValueError("记忆缺少可核对的文件位置和摘要")
            if any(previous.ref == source.ref for previous in record.sources):
                if record.state == "active" and not _past(record.review_after):
                    return record
                raise MemoryConflictError("同一来源引用的文件复核结果不一致")
            if source.order <= record.source_order:
                raise MemoryConflictError("旧文件依据不能确认较新的记忆")
            updated = replace(
                record,
                version=record.version + 1,
                state="active",
                sources=(*record.sources, source),
                updated_at=_now(),
                source_order=source.order,
                confirmed_at=_now(),
                review_after=_next_review_at(scope, "active"),
            )
            data["records"][record_id] = updated.to_dict()
            data["control_epoch"] += 1
            await self._write(scope, data)
            return updated

    async def _set_pin(
        self, scope: MemoryScope, record_id: str, source: MemorySource, *, pinned: bool
    ) -> MemoryRecord:
        if source.kind != "user":
            raise ValueError("固定记忆需要用户来源")
        async with self._lock(scope):
            data = await self._read(scope)
            raw = data["records"].get(record_id)
            if raw is None:
                raise KeyError(record_id)
            record = MemoryRecord.from_dict(raw, scope)
            if record.state == "replaced":
                raise MemoryConflictError("已替代的记忆不能固定")
            if any(previous.ref == source.ref for previous in record.sources):
                if record.pinned == pinned:
                    return record
                raise MemoryConflictError("同一来源引用不能执行不同的固定操作")
            if source.order <= record.source_order:
                raise MemoryConflictError("旧用户来源不能覆盖较新的固定状态")
            updated = replace(
                record,
                version=record.version + 1,
                pinned=pinned,
                sources=(*record.sources, source),
                updated_at=_now(),
                source_order=source.order,
            )
            data["records"][record_id] = updated.to_dict()
            data["control_epoch"] += 1
            await self._write(scope, data)
            return updated

    async def aforget(self, scope: MemoryScope, record_id: str, source: MemorySource) -> None:
        """删除可用正文，并阻止旧来源、旧整理租约重新写回同一主题。"""
        if source.kind != "user":
            raise ValueError("遗忘需要用户来源")
        async with self._lock(scope):
            data = await self._read(scope)
            raw = data["records"].get(record_id)
            if raw is None:
                found = next(
                    (
                        (key, item)
                        for key, item in data["tombstones"].items()
                        if item["record_id"] == record_id
                    ),
                    None,
                )
                if found is None:
                    raise KeyError(record_id)
                digest, previous = found
                if source.order <= previous["source_order"]:
                    return
            else:
                record = MemoryRecord.from_dict(raw, scope)
                if source.order <= record.source_order:
                    raise MemoryConflictError("旧用户来源不能删除较新的记忆")
                del data["records"][record_id]
                digest = _subject_digest(record.subject)
            data["control_epoch"] += 1
            data["tombstones"][digest] = {
                "record_id": record_id,
                "subject_digest": digest,
                "source_order": source.order,
                "control_epoch": data["control_epoch"],
                "forgotten_at": _now(),
            }
            for job_id, raw_job in list(data["pending"].items()):
                job = MemoryJob.from_dict(raw_job)
                if job.source.order <= source.order:
                    del data["pending"][job_id]
                    self._mark_processed(data, job, "forgotten")
            await self._write(scope, data)

    async def aenqueue(self, scope: MemoryScope, source: MemorySource) -> MemoryJob:
        """只保存原始来源引用，重复来源不建立第二份工作。"""
        job_id = self._record_id(scope, source.ref, "job")
        async with self._lock(scope):
            data = await self._read(scope)
            existing = data["pending"].get(job_id)
            if existing is not None:
                job = MemoryJob.from_dict(existing)
                if job.source != source:
                    raise MemoryConflictError("同一来源引用出现不一致的内容")
                return job
            processed = data["processed"].get(source.ref)
            if processed is not None:
                if processed.get("source_sha256") != _source_fingerprint(source):
                    raise MemoryConflictError("同一来源引用出现不一致的内容")
                return MemoryJob(id=job_id, source=source, status="completed")
            if any(
                source.order <= barrier["source_order"]
                for barrier in data["tombstones"].values()
            ):
                self._mark_processed(data, MemoryJob(id=job_id, source=source), "forgotten")
                await self._write(scope, data)
                return MemoryJob(id=job_id, source=source, status="completed")
            job = MemoryJob(id=job_id, source=source)
            data["pending"][job_id] = job.to_dict()
            await self._write(scope, data)
            return job

    async def aclaim(
        self,
        scope: MemoryScope,
        worker_id: str,
        *,
        project_id: str | None = None,
        source_ref: str | None = None,
        lease_seconds: int = 180,
    ) -> MemoryJob | None:
        """过期租约可由新进程领取；模型调用阶段不持锁。"""
        if not worker_id or lease_seconds <= 0:
            raise ValueError("领取整理工作需要 worker_id 与正数租约")

        def matches_project(value: Mapping[str, Any]) -> bool:
            return (
                (project_id is None or value["source"].get("project_id", "") == project_id)
                and (source_ref is None or value["source"].get("ref") == source_ref)
            )

        async with self._lock(scope):
            data = await self._read(scope)
            if any(
                matches_project(value)
                and value["status"] == "claimed"
                and not _past(value.get("lease_until"))
                for value in data["pending"].values()
            ):
                return None
            available = sorted(
                (
                    MemoryJob.from_dict(value)
                    for value in data["pending"].values()
                    if matches_project(value)
                    and (
                        value["status"] == "pending"
                        or (value["status"] == "claimed" and _past(value.get("lease_until")))
                    )
                ),
                key=lambda job: (job.source.order, job.id),
            )
            if not available:
                return None
            current = available[0]
            claimed = replace(
                current,
                status="claimed",
                lease_token=uuid.uuid4().hex,
                lease_owner=worker_id,
                lease_until=(datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(),
                attempts=current.attempts + 1,
            )
            data["pending"][claimed.id] = claimed.to_dict()
            await self._write(scope, data)
            return claimed

    async def arelease(self, scope: MemoryScope, job: MemoryJob, error: str = "") -> bool:
        """失败或取消后释放租约，保留来源供下次启动继续。"""
        if not job.lease_token:
            return False
        async with self._lock(scope):
            data = await self._read(scope)
            raw = data["pending"].get(job.id)
            if (
                raw is None
                or raw.get("status") != "claimed"
                or raw.get("lease_token") != job.lease_token
            ):
                return False
            released = replace(
                MemoryJob.from_dict(raw),
                status="pending",
                lease_token="",
                lease_owner="",
                lease_until=None,
                last_error=_safe_error_code(error),
            )
            data["pending"][job.id] = released.to_dict()
            await self._write(scope, data)
            return True

    async def ainvalidate(
        self, scope: MemoryScope, job: MemoryJob, reason: str = "cancelled"
    ) -> bool:
        """撤销已获租约的来源；关闭学习或切只读后不得重新整理。"""
        if not job.lease_token:
            return False
        async with self._lock(scope):
            data = await self._read(scope)
            raw = data["pending"].get(job.id)
            if raw is None:
                return False
            current = MemoryJob.from_dict(raw)
            if (
                current.status != "claimed"
                or current.lease_token != job.lease_token
                or current.source != job.source
                or _past(current.lease_until)
            ):
                return False
            del data["pending"][job.id]
            self._mark_processed(data, current, "cancelled")
            data["processed"][current.source.ref]["reason"] = _safe_error_code(reason)
            await self._write(scope, data)
            return True

    async def arevoke(self, scope: MemoryScope, source_refs: set[str]) -> int:
        """一次撤销指定来源的所有待整理工作，包括已被其他进程领取的工作。"""
        if not source_refs:
            return 0
        if any(not ref for ref in source_refs):
            raise ValueError("撤销来源引用不能为空")
        async with self._lock(scope):
            data = await self._read(scope)
            revoked = 0
            for job_id, raw in list(data["pending"].items()):
                job = MemoryJob.from_dict(raw)
                if job.source.ref not in source_refs:
                    continue
                del data["pending"][job_id]
                self._mark_processed(data, job, "cancelled")
                revoked += 1
            if revoked:
                await self._write(scope, data)
            return revoked

    async def acommit(
        self,
        scope: MemoryScope,
        job: MemoryJob,
        snapshot: MemorySnapshot,
        changes: Sequence[MemoryChange],
    ) -> bool:
        """核对来源、控制代数和涉及条目的版本后，单次写入全部修订。"""
        if snapshot.scope != scope:
            raise ValueError("整理快照属于其他作用域")
        async with self._lock(scope):
            data = await self._read(scope)
            raw_job = data["pending"].get(job.id)
            stored_job = MemoryJob.from_dict(raw_job) if raw_job is not None else None
            if (
                stored_job is None
                or stored_job.status != "claimed"
                or stored_job.lease_token != job.lease_token
                or stored_job.source != job.source
                or not job.lease_token
                or _past(stored_job.lease_until)
            ):
                return False
            if data["control_epoch"] != snapshot.control_epoch:
                await self._release_conflict(scope, data, job)
                return False
            for change in changes:
                if not set(change.evidence_refs).issubset(job.source.evidence_refs):
                    raise ValueError("记忆建议引用了未授权的原始依据")
                if change.action not in ("insert", "update", "replace", "retire"):
                    raise ValueError("未知的记忆修订动作")
                if change.state not in (
                    "active",
                    "candidate",
                    "needs_verification",
                    "replaced",
                ):
                    raise ValueError("未知的记忆状态")
                if change.action != "insert":
                    previous = snapshot.records.get(change.record_id)
                    current = data["records"].get(change.record_id)
                    if previous is None or current is None or current["version"] != previous.version:
                        if current is not None and job.source.order <= current["source_order"]:
                            await self._supersede(scope, data, stored_job)
                        else:
                            await self._release_conflict(scope, data, job)
                        return False
                    if job.source.order <= current["source_order"]:
                        await self._supersede(scope, data, stored_job)
                        return False
                else:
                    same_subject = next(
                        (
                            value
                            for value in data["records"].values()
                            if value["state"] != "replaced"
                            and _subject_key(str(value["subject"]))
                            == _subject_key(change.subject)
                        ),
                        None,
                    )
                    if same_subject is not None:
                        if job.source.order <= same_subject["source_order"]:
                            await self._supersede(scope, data, stored_job)
                        else:
                            await self._release_conflict(scope, data, job)
                        return False

            # 先在局部数据上构造全部变化；任何校验失败都不调用 Store。
            updated = {
                **data,
                "records": dict(data["records"]),
                "pending": dict(data["pending"]),
                "processed": dict(data["processed"]),
                "tombstones": dict(data["tombstones"]),
            }
            for index, change in enumerate(changes):
                source = replace(job.source, evidence_refs=change.evidence_refs)
                if change.action in ("update", "replace", "retire"):
                    current = MemoryRecord.from_dict(updated["records"][change.record_id], scope)
                    if change.action == "retire":
                        revised = replace(
                            current,
                            version=current.version + 1,
                            state="replaced",
                            sources=(*current.sources, source),
                            updated_at=_now(),
                            source_order=source.order,
                        )
                    elif change.action == "update":
                        if change.subject and _subject_key(change.subject) != _subject_key(current.subject):
                            self._check_subject_barrier(
                                updated,
                                change.subject,
                                source,
                                reaffirmed=change.reaffirmed,
                            )
                        revised = replace(
                            current,
                            version=current.version + 1,
                            subject=change.subject.strip() or current.subject,
                            text=change.text.strip() or current.text,
                            state=change.state,
                            sources=(*current.sources, source),
                            applicability=dict(change.applicability) or current.applicability,
                            updated_at=_now(),
                            source_order=source.order,
                            confirmed_at=current.confirmed_at,
                            review_after=(
                                _next_review_at(scope, change.state)
                                if change.state == "active"
                                else current.review_after
                            ),
                        )
                    else:
                        revised = replace(
                            current,
                            version=current.version + 1,
                            state="replaced",
                            sources=(*current.sources, source),
                            updated_at=_now(),
                            source_order=source.order,
                        )
                    updated["records"][current.id] = revised.to_dict()
                if change.action in ("insert", "replace"):
                    self._check_subject_barrier(
                        updated, change.subject, source, reaffirmed=change.reaffirmed
                    )
                    new_id = self._record_id(
                        scope, job.id, change.candidate_key or f"{index}:{_subject_key(change.subject)}"
                    )
                    if new_id in updated["records"]:
                        raise MemoryConflictError("本批次记忆候选标识重复")
                    record = self._new_record(
                        scope,
                        new_id,
                        change.subject,
                        change.text,
                        source,
                        change.state,
                        change.applicability,
                    )
                    updated["records"][new_id] = record.to_dict()
            del updated["pending"][job.id]
            self._mark_processed(updated, stored_job, "completed")
            await self._write(scope, updated)
            return True

    async def _release_conflict(
        self, scope: MemoryScope, data: dict[str, Any], job: MemoryJob
    ) -> None:
        current = MemoryJob.from_dict(data["pending"][job.id])
        data["pending"][job.id] = replace(
            current, status="pending", lease_token="", lease_owner="", lease_until=None
        ).to_dict()
        await self._write(scope, data)

    async def _supersede(
        self, scope: MemoryScope, data: dict[str, Any], job: MemoryJob
    ) -> None:
        """旧来源已被新事实覆盖，结束它以免恢复后无限重试。"""
        del data["pending"][job.id]
        self._mark_processed(data, job, "superseded")
        await self._write(scope, data)


__all__ = ["MemoryRepository"]
