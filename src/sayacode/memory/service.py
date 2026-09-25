"""应用级记忆入口：用户设置、查看与直接记忆操作。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..config import Profile
from .evidence import collect_turn_evidence
from .privacy import contains_secret, redact_secrets
from .processing import MemoryLearningCoordinator
from .records import (
    MemoryConflictError,
    MemoryRecord,
    MemoryScope,
    MemorySource,
)
from .repository import MemoryRepository
from .retrieval import MemoryRetriever, _file_digest, _workspace_file

if TYPE_CHECKING:
    from ..agent import AgentContext, AgentHandle
    from ..application import SayacodeApp


def model_identity_sha256(profile: Profile) -> str:
    """只保存模型端点和凭据的指纹，避免待处理材料发往变化后的端点。"""
    identity = (profile.protocol, profile.base_url, profile.model_id, profile.api_key)
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def _manual_source(thread_id: str) -> MemorySource:
    """用户直接操作的顺序以本机收到命令的时间为准。"""
    return MemorySource(
        ref=f"user-command-{uuid4().hex}",
        kind="user",
        order=f"{datetime.now(UTC).isoformat()}:{uuid4().hex}",
        thread_id=thread_id,
    )


def _file_check_source(thread_id: str) -> MemorySource:
    """本机文件摘要复核有自己的来源，不冒充用户确认。"""
    return MemorySource(
        ref=f"file-check-{uuid4().hex}",
        kind="tool",
        order=f"{datetime.now(UTC).isoformat()}:{uuid4().hex}",
        thread_id=thread_id,
    )


class MemoryService:
    """围绕当前应用复用一份 Store，并交由协调对象管理自动整理。"""

    def __init__(self, app: SayacodeApp) -> None:
        self.app = app
        self.repository = MemoryRepository(app.runtime.store, app.paths.home)
        self.retriever = MemoryRetriever(self.repository)
        self._recent: dict[str, dict[str, dict[str, Any]]] = {}
        self.learning = MemoryLearningCoordinator(
            app, self.repository, self._configured_keys, model_identity_sha256
        )

    def _scopes(self) -> tuple[MemoryScope, MemoryScope]:
        return self.repository.scopes_for(self.app.workspace)

    def _configured_keys(self) -> tuple[str, ...]:
        keys = [profile.api_key for profile in self.app.config.profiles.values()]
        if self.app.profile_override is not None:
            keys.append(self.app.profile_override.api_key)
        return tuple(key for key in keys if key)

    async def settings(self, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        """查看或原子保存用户安装级记忆设置。"""
        if updates is not None:
            explicit = dict(updates)
            if explicit.get("enabled") is False or (
                "learn" in explicit and explicit["learn"] != "auto"
            ):
                explicit["revoked_before"] = datetime.now(UTC).isoformat()
            updated, previous = await self.app.repository.update_memory(explicit)
            await self.app.repository.refresh(self.app.config)
            settings = updated.memory
            self.app._handles.clear()
            if settings.revoked_before != previous.revoked_before:
                await self.learning.revoke_pending()
            if settings.enabled and (not previous.enabled or previous.learn != settings.learn):
                await self.resume_pending()
        return asdict(self.app.config.memory)

    async def session_settings(
        self, thread_id: str | None = None, updates: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """会话可独立暂停读取或学习，空覆盖继承安装级设置。"""
        tid = thread_id or self.app.session_id
        thread: dict[str, Any] | None
        if updates is not None:
            if set(updates) - {"use", "learn"}:
                raise ValueError("会话记忆只支持 use 与 learn 覆盖")
            if "use" in updates and updates["use"] is not None and not isinstance(
                updates["use"], bool
            ):
                raise ValueError("会话 use 覆盖须为 on、off 或 default")
            if "learn" in updates and updates["learn"] is not None and updates["learn"] not in {
                "off",
                "explicit",
                "auto",
            }:
                raise ValueError("会话 learn 覆盖须为 off、explicit、auto 或 default")
            transition = {"revoke": False, "resume": False}

            def changed_fields(current: dict[str, Any]) -> dict[str, Any]:
                old_use = current.get("memory_use_override")
                old_learn = current.get("memory_learn_override")
                use_override = updates["use"] if "use" in updates else old_use
                learn_override = updates["learn"] if "learn" in updates else old_learn
                patch: dict[str, Any] = {}
                if "use" in updates and use_override != old_use:
                    patch["memory_use_override"] = use_override
                if "learn" in updates and learn_override != old_learn:
                    patch["memory_learn_override"] = learn_override

                def effective(value: str | None) -> str:
                    if not self.app.config.memory.enabled:
                        return "off"
                    return self.app.config.memory.learn if value is None else value

                before = effective(old_learn)
                after = effective(learn_override)
                transition["revoke"] = before == "auto" and after != "auto"
                transition["resume"] = before != "auto" and after == "auto"
                if transition["revoke"]:
                    patch["memory_revoked_before"] = datetime.now(UTC).isoformat()
                return patch

            thread = await self.app.runtime.update_thread(tid, changed_fields)
            self.app._handles.clear()
            if transition["revoke"]:
                await self.learning.revoke_pending(thread_id=tid)
            if transition["resume"]:
                await self.resume_pending()
        else:
            thread = await self.app.runtime.get_thread(tid)
        use = thread.get("memory_use_override") if thread else None
        learn = thread.get("memory_learn_override") if thread else None
        enabled = self.app.config.memory.enabled
        return {
            "thread_id": tid,
            "use_override": use,
            "learn_override": learn,
            "use": bool(enabled and (self.app.config.memory.use if use is None else use)),
            "learn": self.app.config.memory.learn if enabled and learn is None else (
                learn if enabled else "off"
            ),
        }

    async def status(self) -> dict[str, Any]:
        scopes = self._scopes()
        snapshots = await asyncio.gather(*(self.repository.aread(scope) for scope in scopes))
        counts = {"active": 0, "candidate": 0, "needs_verification": 0, "expired": 0}
        pending = 0
        for snapshot in snapshots:
            views = await asyncio.gather(
                *(self._view_record(record) for record in snapshot.records.values())
            )
            for view in views:
                state = str(view["effective_state"])
                if state in counts:
                    counts[state] += 1
            pending += len(snapshot.pending)
        return {
            "settings": await self.settings(),
            "session": await self.session_settings(),
            "counts": counts,
            "pending": pending,
            "active_jobs": len(self.learning.tasks.active_keys),
            "failed_jobs": len(self.learning.tasks.failed_keys),
            "retrieval": "basic",
        }

    async def list(self, scope: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        """列出本安装与当前项目可见的全部记忆状态。"""
        if scope not in {None, "user", "project"}:
            raise ValueError("记忆范围只能是 user 或 project")
        scopes = tuple(item for item in self._scopes() if scope is None or item.kind == scope)
        results = await asyncio.gather(
            *(self.repository.alist(item, include_inactive=True) for item in scopes)
        )
        asked = (query or "").casefold().strip()
        selected = [
            record
            for records in results
            for record in records
            if not asked or asked in f"{record.subject} {record.text}".casefold()
        ]
        return list(await asyncio.gather(*(self._view_record(record) for record in selected)))

    async def _view_record(self, record: MemoryRecord) -> dict[str, Any]:
        match = await self.retriever._classify(record, self.app.workspace)
        effective: str = record.state
        if record.state == "active" and not match.current:
            effective = "expired" if match.reason == "明确有效期已过" else "needs_verification"
        return {
            **record.to_dict(),
            "effective_state": effective,
            "validity_reason": match.reason,
        }

    async def _find(self, record_id: str) -> MemoryRecord:
        for scope in self._scopes():
            if record := await self.repository.aget(scope, record_id):
                return record
        raise KeyError(f"未找到记忆：{record_id}")

    async def get(self, record_id: str) -> dict[str, Any]:
        record = await self._find(record_id)
        result = await self._view_record(record)
        excerpts: list[dict[str, str]] = []
        for source in record.sources[-3:]:
            if not source.checkpoint_id or not source.thread_id:
                continue
            try:
                thread = await self.app.runtime.get_thread(source.thread_id)
                if thread is None or Path(str(thread.get("workspace") or "")).resolve() != self.app.workspace:
                    continue
                handle, _ = await self.app._context_for_thread(source.thread_id)
                evidence = await collect_turn_evidence(
                    self.app.runtime,
                    handle,
                    source,
                    user_message_id=source.user_message_id,
                )
                if not set(source.evidence_refs).issubset(evidence.source_ids):
                    continue
            except (KeyError, RuntimeError, ValueError):
                continue
            for message_id, message in zip(
                evidence.source_ids, evidence.messages, strict=True
            ):
                if message_id not in source.evidence_refs or not isinstance(message.content, str):
                    continue
                excerpts.append(
                    {
                        "source_ref": source.ref,
                        "message_id": message_id,
                        "role": message.type,
                        "preview": redact_secrets(message.content, self._configured_keys())[:500],
                    }
                )
        result["evidence"] = excerpts
        return result

    def begin_turn(self, thread_id: str) -> None:
        self._recent[thread_id] = {}

    def record_references(self, thread_id: str, matches: Sequence[Any]) -> None:
        """只记录本轮向模型提供过的 ID，不推断模型实际采用。"""
        recent = self._recent.setdefault(thread_id, {})
        for match in matches:
            recent[match.record.id] = {
                **match.record.to_dict(),
                "match_reason": match.reason,
                "provided_to_model": True,
            }

    async def recent(self, thread_id: str | None = None) -> Sequence[dict[str, Any]]:
        tid = thread_id or self.app.session_id
        return list(self._recent.get(tid, {}).values())

    async def remember(self, text: str, scope: str = "user") -> dict[str, Any]:
        if scope not in {"user", "project"} or not text.strip():
            raise ValueError("请指定 user 或 project 以及要记住的内容")
        if not self.app.config.memory.enabled:
            # 明确保存一条记忆意味着今后要使用它，不隐式开启后台模型整理。
            await self.settings({"enabled": True, "learn": "explicit"})
        selected = next(item for item in self._scopes() if item.kind == scope)
        body = text.strip()
        if contains_secret(body, self._configured_keys()):
            raise ValueError("长期记忆不能保存凭据")
        subject = body.splitlines()[0][:80]
        record = await self.repository.aremember(
            selected,
            subject,
            body,
            _manual_source(self.app.session_id),
        )
        return record.to_dict()

    async def correct(self, record_id: str, text: str) -> dict[str, Any]:
        if contains_secret(text, self._configured_keys()):
            raise ValueError("长期记忆不能保存凭据")
        record = await self._find(record_id)
        changed = await self.repository.acorrect(
            record.scope, record_id, text, _manual_source(self.app.session_id)
        )
        return changed.to_dict()

    async def confirm(self, record_id: str) -> dict[str, Any]:
        record = await self._find(record_id)
        confirmed = await self.repository.aconfirm(
            record.scope, record_id, _manual_source(self.app.session_id)
        )
        return confirmed.to_dict()

    async def forget(self, record_id: str) -> dict[str, Any]:
        record = await self._find(record_id)
        await self.repository.aforget(
            record.scope, record_id, _manual_source(self.app.session_id)
        )
        return {"forgotten": record_id, "scope": record.scope.kind}

    async def refresh(self, record_id: str | None = None) -> dict[str, Any]:
        """仅对有当前文件依据的记录做自动确认。"""
        targets = [await self._find(record_id)] if record_id else [
            record
            for scope in self._scopes()
            for record in await self.repository.alist(scope, include_inactive=True)
            if record.state == "needs_verification"
        ]
        confirmed: list[str] = []
        unresolved: list[str] = []
        for record in targets:
            conditions = record.applicability
            target = (
                _workspace_file(self.app.workspace, conditions["path"])
                if record.scope.kind == "project" and "path" in conditions
                else None
            )
            expected = conditions.get("sha256")
            if target is None or not expected:
                unresolved.append(record.id)
                continue
            if await asyncio.to_thread(_file_digest, target) != expected:
                unresolved.append(record.id)
                continue
            try:
                await self.repository.arefresh_from_file(
                    record.scope, record.id, _file_check_source(self.app.session_id)
                )
            except MemoryConflictError:
                unresolved.append(record.id)
            else:
                confirmed.append(record.id)
        return {"confirmed": confirmed, "requires_user_review": unresolved}

    async def pin(self, record_id: str, *, pinned: bool = True) -> dict[str, Any]:
        record = await self._find(record_id)
        source = _manual_source(self.app.session_id)
        result = (
            await self.repository.apin(record.scope, record.id, source)
            if pinned
            else await self.repository.aunpin(record.scope, record.id, source)
        )
        return result.to_dict()

    async def propose(
        self,
        *,
        subject: str,
        text: str,
        scope: str,
        source: MemorySource,
    ) -> dict[str, Any]:
        """工具只可提交候选，不能把模型自己的建议标成用户确认。"""
        if scope not in {"user", "project"}:
            raise ValueError("记忆范围只能是 user 或 project")
        if source.kind != "user":
            return {"accepted": False, "reason": "模型建议需在后台取证后整理"}
        if contains_secret(f"{subject}\n{text}", self._configured_keys()):
            return {"accepted": False, "reason": "长期记忆不能保存凭据"}
        selected = next(item for item in self._scopes() if item.kind == scope)
        record = await self.repository.aremember(
            selected, subject, text, source, state="candidate"
        )
        return {"candidate": record.id, "scope": selected.kind}

    async def on_turn_complete(
        self, handle: AgentHandle, context: AgentContext, *, schedule: bool = True
    ) -> None:
        await self.learning.on_turn_complete(handle, context, schedule=schedule)

    async def on_turn_failed(
        self, handle: AgentHandle, context: AgentContext, *, schedule: bool = True
    ) -> None:
        await self.learning.on_turn_failed(handle, context, schedule=schedule)

    async def flush_headless(self, thread_id: str) -> None:
        await self.learning.flush_headless(thread_id)

    async def resume_pending(self) -> None:
        await self.learning.resume_pending()

    async def drain(self, timeout: float) -> tuple[str, ...]:
        return await self.learning.drain(timeout)


__all__ = ["MemoryService", "model_identity_sha256"]
