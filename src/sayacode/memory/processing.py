"""长期记忆来源调度、提取提交和进程内异步生命周期。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping, MutableSequence, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import ToolMessage

from ..agent import models
from ..config import Profile
from .evidence import TurnEvidence, collect_turn_evidence
from .learning import MemoryLearner, MemoryLearningTasks
from .privacy import contains_secret, extraction_evidence, redact_secrets
from .records import MemoryChange, MemoryJob, MemoryScope, MemorySource
from .repository import MemoryRepository
from .retrieval import _file_digest, _workspace_file

if TYPE_CHECKING:
    from ..agent import AgentContext, AgentHandle
    from ..application import SayacodeApp


def _at_or_before(value: str, barrier: str | None) -> bool:
    """时间无法核对时保守地拒绝旧来源，避免撤销后重新学习。"""
    if barrier is None:
        return False
    try:
        return datetime.fromisoformat(value) <= datetime.fromisoformat(barrier)
    except (TypeError, ValueError):
        return True


class MemoryLearningCoordinator:
    """同一 Store 上的进程内整理任务；持久来源仍由 Repository 管理。"""

    def __init__(
        self,
        app: SayacodeApp,
        repository: MemoryRepository,
        configured_keys: Callable[[], tuple[str, ...]],
        profile_identity: Callable[[Profile], str],
    ) -> None:
        self.app = app
        self.repository = repository
        self._configured_keys = configured_keys
        self._profile_identity = profile_identity
        self.tasks = MemoryLearningTasks()
        self._last_activity: dict[str, float] = {}
        self._headless_sources: dict[str, list[tuple[MemoryScope, str]]] = {}
        self._worker_id = f"cli-{uuid4().hex}"
        self._learner_factory = MemoryLearner

    def _scopes(self) -> tuple[MemoryScope, MemoryScope]:
        return self.repository.scopes_for(self.app.workspace)

    async def revoke_pending(self, *, thread_id: str | None = None) -> None:
        """关闭学习时撤销 Store 中的旧来源，包括其他工作区已领取的任务。"""
        offset = 0
        while True:
            page = await self.app.runtime.store.alist_namespaces(
                prefix=("sayacode", "memory"), max_depth=4, limit=100, offset=offset
            )
            for namespace in page:
                if len(namespace) != 4 or namespace[2] not in {"user", "project"}:
                    continue
                scope = (
                    MemoryScope("user", namespace[3])
                    if namespace[2] == "user"
                    else MemoryScope("project", namespace[3])
                )
                snapshot = await self.repository.aread(scope)
                refs = {
                    job.source.ref
                    for job in snapshot.pending.values()
                    if thread_id is None or job.source.thread_id == thread_id
                }
                if refs:
                    await self.repository.arevoke(scope, refs)
            if len(page) < 100:
                return
            offset += len(page)

    async def on_turn_complete(
        self, handle: AgentHandle, context: AgentContext, *, schedule: bool = True
    ) -> None:
        """图完成后先保存可恢复来源，再让进程内任务在空闲时整理。"""
        if (
            not self.app.config.memory.enabled
            or context.trust_level == "read_only"
            or not context.memory_learning_enabled
            or context.is_background
            or context.task_id is not None
        ):
            return
        snapshot = await self.app.runtime.get_state(handle, context.session_id)
        await self._enqueue_snapshot(handle, context, snapshot, schedule=schedule)

    async def on_turn_failed(
        self, handle: AgentHandle, context: AgentContext, *, schedule: bool = True
    ) -> None:
        """运行失败仍保留本轮用户原话中的明确偏好，模型错误正文不作依据。"""
        if (
            not self.app.config.memory.enabled
            or not context.memory_learning_enabled
            or context.trust_level == "read_only"
            or context.is_background
            or context.task_id is not None
        ):
            return
        snapshot = await self.app.runtime.get_state(handle, context.session_id)
        await self._enqueue_snapshot(
            handle, context, snapshot, schedule=schedule, allow_incomplete=True
        )

    async def _enqueue_snapshot(
        self,
        handle: AgentHandle,
        context: AgentContext,
        snapshot: Any,
        *,
        schedule: bool,
        allow_incomplete: bool = False,
    ) -> None:
        """从一张已完成检查点建立幂等来源，不依赖线程目录的更新时间。"""
        if snapshot.interrupts or (snapshot.next and not allow_incomplete):
            return
        marker = snapshot.values.get("memory_turn") if snapshot.values else None
        if not isinstance(marker, Mapping) or not marker.get("eligible"):
            return
        if (
            marker.get("thread_id") != context.session_id
            or marker.get("owner_id") != context.memory_owner_id
            or marker.get("project_id") != context.memory_project_id
        ):
            return
        thread = await self.app.runtime.get_thread(context.session_id)
        received_at = str(marker.get("received_at") or "")
        if not received_at:
            return
        if _at_or_before(received_at, self.app.config.memory.revoked_before) or _at_or_before(
            received_at,
            str(thread.get("memory_revoked_before"))
            if thread and thread.get("memory_revoked_before")
            else None,
        ):
            return
        checkpoint_id = str(snapshot.config.get("configurable", {}).get("checkpoint_id") or "")
        human_id = str(marker.get("message_id") or "")
        if not checkpoint_id or not human_id:
            return
        source_ref = f"turn:{context.session_id}:{human_id}"
        source = MemorySource(
            ref=source_ref,
            kind="user",
            order=f"{marker.get('received_at', '')}:{human_id}",
            thread_id=context.session_id,
            checkpoint_id=checkpoint_id,
            user_message_id=human_id,
            project_id=context.memory_project_id,
            profile_name=str(marker.get("profile_name") or ""),
            model_identity_sha256=str(marker.get("model_identity_sha256") or ""),
        )
        scopes = (
            MemoryScope("user", context.memory_owner_id),
            MemoryScope("project", context.memory_project_id),
        )
        unseen: list[MemoryScope] = []
        for scope in scopes:
            stored = await self.repository.aread(scope)
            if source_ref in stored.processed or any(
                job.source.ref == source_ref for job in stored.pending.values()
            ):
                continue
            unseen.append(scope)
        if not unseen:
            return
        evidence = await collect_turn_evidence(
            self.app.runtime, handle, source, user_message_id=human_id
        )
        source = replace(source, evidence_refs=evidence.source_ids)
        for scope in unseen:
            job = await self.repository.aenqueue(scope, source)
            if job.status != "completed":
                if schedule:
                    self._schedule_scope(scope)
                else:
                    self._headless_sources.setdefault(context.session_id, []).append(
                        (scope, source.ref)
                    )

    def _schedule_scope(self, scope: MemoryScope) -> None:
        """同作用域连续轮次共用一个空闲计时任务。"""
        key = "/".join(scope.namespace)
        self._last_activity[key] = time.monotonic()
        self.tasks.schedule(key, lambda: self._work_scope(scope))

    async def _wait_until_idle(self, scope: MemoryScope) -> None:
        key = "/".join(scope.namespace)
        while self.app.config.memory.enabled:
            remaining = self.app.config.memory.idle_seconds - (
                time.monotonic() - self._last_activity.get(key, 0)
            )
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    def _profile_for_source(self, source: MemorySource) -> Profile | None:
        profile = self.app.config.profiles.get(source.profile_name)
        if profile is None and self.app.profile_override is not None:
            if self.app.profile_override.name == source.profile_name:
                profile = self.app.profile_override
        if profile is None or self._profile_identity(profile) != source.model_identity_sha256:
            return None
        return profile

    async def _source_evidence(
        self, source: MemorySource
    ) -> tuple[TurnEvidence, Any]:
        """从来源所属线程重建原始消息，不读取当前活动会话的历史。"""
        thread = await self.app.runtime.get_thread(source.thread_id)
        if thread is None or thread.get("is_background"):
            raise ValueError("来源线程已不存在或不是主会话")
        if Path(str(thread.get("workspace") or "")).resolve() != self.app.workspace:
            raise ValueError("来源不属于当前 CLI 工作区，等待该工作区重新启动")
        handle, _ = await self.app._context_for_thread(source.thread_id)
        evidence = await collect_turn_evidence(
            self.app.runtime,
            handle,
            source,
            user_message_id=source.user_message_id,
        )
        if not set(evidence.source_ids).issubset(source.evidence_refs):
            raise ValueError("重建的来源与保存时的证据引用不一致")
        return evidence, handle

    @staticmethod
    def _valid_changes(
        changes: Sequence[MemoryChange],
        evidence: TurnEvidence,
        workspace: Path,
        configured_keys: Sequence[str] = (),
    ) -> Sequence[MemoryChange]:
        """文件条件由本机计算；模型不能自行声明某路径已经验证。"""
        verified = set(evidence.verified_source_ids)
        messages = dict(zip(evidence.source_ids, evidence.messages, strict=True))
        checked: MutableSequence[MemoryChange] = []
        for change in changes:
            if contains_secret(f"{change.subject}\n{change.text}", configured_keys):
                continue
            applicability: dict[str, str] = {}
            observed_file_changed = False
            for source_id in change.evidence_refs:
                message = messages.get(source_id)
                if (
                    not isinstance(message, ToolMessage)
                    or message.name != "read_file"
                    or message.status != "success"
                ):
                    continue
                call = evidence.tool_calls.get(str(message.tool_call_id))
                arguments = call.get("args", {}) if call else {}
                relative_path = arguments.get("path") if isinstance(arguments, dict) else None
                if not isinstance(relative_path, str):
                    continue
                target = _workspace_file(workspace, relative_path)
                if target is None:
                    continue
                try:
                    result = json.loads(message.content) if isinstance(message.content, str) else {}
                except json.JSONDecodeError:
                    result = {}
                read_digest = result.get("sha256") if isinstance(result, dict) else None
                if (
                    not isinstance(read_digest, str)
                    or len(read_digest) != 64
                    or any(char not in "0123456789abcdef" for char in read_digest)
                ):
                    continue
                applicability = {
                    "path": str(target.relative_to(workspace.resolve())),
                    "sha256": read_digest,
                }
                observed_file_changed = _file_digest(target) != read_digest
                break
            if observed_file_changed:
                change = replace(change, state="needs_verification")
            if change.state == "active" and not any(
                source_id in verified for source_id in change.evidence_refs
            ):
                # 用户原话可成为明确偏好，工具和助手的单方推断先做候选。
                if not any(
                    source_id in messages and messages[source_id].type == "human"
                    for source_id in change.evidence_refs
                ):
                    change = replace(change, state="candidate")
            checked.append(replace(change, applicability=applicability))
        return checked

    async def _work_scope(
        self, scope: MemoryScope, *, source_ref: str | None = None, wait_for_idle: bool = True
    ) -> None:
        """一次只整理当前项目可处理的工作；冲突时留待下次重试。"""
        if wait_for_idle:
            await self._wait_until_idle(scope)
        await self.app.repository.refresh(self.app.config)
        if not self.app.config.memory.enabled:
            return
        while True:
            job = await self._claim_local(scope, source_ref)
            if job is None:
                return
            try:
                if not self.app.config.memory.enabled:
                    await self.repository.ainvalidate(scope, job)
                    return
                if not await self._source_can_learn(job.source):
                    await self.repository.ainvalidate(scope, job)
                    return
                profile = self._profile_for_source(job.source)
                if profile is None:
                    await self.repository.arelease(scope, job, "model_changed")
                    return
                evidence, _ = await self._source_evidence(job.source)
                safe_evidence = extraction_evidence(evidence, self._configured_keys())
                snapshot = await self.repository.aread(scope)
                override = (
                    self.app.model_override
                    if profile.name == self.app.profile_name
                    and self.app.config.memory.model_profile is None
                    else None
                )
                model: BaseChatModel = models.model_for(profile, override)
                extractor = self._learner_factory(model)
                audit_callback = self.app._audit_callback(
                    job.source.thread_id, task_id=f"memory:{job.id}"
                )
                changes = await extractor.extract(
                    safe_evidence.messages,
                    [
                        replace(record, text=redact_secrets(record.text, self._configured_keys()))
                        for record in snapshot.records.values()
                    ],
                    scope_kind=scope.kind,
                    source_ids=safe_evidence.source_ids,
                    verified_source_ids=safe_evidence.verified_source_ids,
                    callbacks=[audit_callback],
                )
                validated_changes = self._valid_changes(
                    changes, safe_evidence, self.app.workspace, self._configured_keys()
                )
                pinned_ids = {record.id for record in snapshot.records.values() if record.pinned}
                pinned_subjects = {
                    record.subject.casefold().strip()
                    for record in snapshot.records.values()
                    if record.pinned
                }
                validated_changes = tuple(
                    change
                    for change in validated_changes
                    if change.record_id not in pinned_ids
                    and not (
                        change.action == "insert"
                        and change.subject.casefold().strip() in pinned_subjects
                    )
                )
                await self.app.repository.refresh(self.app.config)
                if not self.app.config.memory.enabled:
                    await self.repository.ainvalidate(scope, job)
                    return
                if not await self._source_can_learn(job.source):
                    await self.repository.ainvalidate(scope, job)
                    return
                committed = await self.repository.acommit(scope, job, snapshot, validated_changes)
                if not committed:
                    return
                if validated_changes:
                    await self.app._notifications.put(
                        {
                            "type": "memory.updated",
                            "thread_id": job.source.thread_id,
                            "scope": scope.kind,
                            "count": len(validated_changes),
                            "source_ref": job.source.ref,
                        }
                    )
                if source_ref is not None:
                    return
            except asyncio.CancelledError:
                await asyncio.shield(self.repository.arelease(scope, job, "cancelled"))
                raise
            except Exception as exc:
                await self.repository.arelease(scope, job, "extract_failed")
                await self.app.audit.append(
                    "memory.failed",
                    thread_id=job.source.thread_id,
                    details={"job_id": job.id, "error_type": type(exc).__name__},
                )
                await self.app._notifications.put(
                    {
                        "type": "memory.failed",
                        "thread_id": job.source.thread_id,
                        "job_id": job.id,
                        "error_type": type(exc).__name__,
                    }
                )
                return

    async def _source_can_learn(self, source: MemorySource) -> bool:
        """读取最新会话档位，旧运行时上下文不代表当前授权。"""
        thread = await self.app.runtime.get_thread(source.thread_id)
        received_at = source.order.rsplit(":", 1)[0]
        return bool(
            thread is not None
            and not thread.get("is_background")
            and thread.get("trust_level") != "read_only"
            and (
                thread.get("memory_learn_override") or self.app.config.memory.learn
            ) == "auto"
            and not _at_or_before(received_at, self.app.config.memory.revoked_before)
            and not _at_or_before(received_at, thread.get("memory_revoked_before"))
        )

    async def _claim_local(
        self, scope: MemoryScope, source_ref: str | None
    ) -> MemoryJob | None:
        """用户作用域可能含同仓库其他路径的来源，只领取本工作区的线程。"""
        snapshot = await self.repository.aread(scope)
        pending = sorted(snapshot.pending.values(), key=lambda item: (item.source.order, item.id))
        for candidate in pending:
            if source_ref is not None and candidate.source.ref != source_ref:
                continue
            if candidate.source.project_id != self._scopes()[1].identity:
                continue
            thread = await self.app.runtime.get_thread(candidate.source.thread_id)
            if thread is None or Path(str(thread.get("workspace") or "")).resolve() != self.app.workspace:
                continue
            claimed = await self.repository.aclaim(
                scope,
                self._worker_id,
                project_id=candidate.source.project_id,
                source_ref=candidate.source.ref,
            )
            if claimed is not None:
                return claimed
        return None

    async def flush_headless(self, thread_id: str) -> None:
        """只处理本次无交互轮次的来源；超时或失败保留持久工作。"""
        sources = list(dict.fromkeys(self._headless_sources.pop(thread_id, [])))
        if not sources:
            return
        work = [
            asyncio.create_task(self._work_scope(scope, source_ref=ref, wait_for_idle=False))
            for scope, ref in sources
        ]
        timeout = self.app.config.memory.headless_timeout_seconds
        try:
            await asyncio.wait_for(asyncio.gather(*work), timeout=timeout)
        except TimeoutError:
            for task in work:
                task.cancel()
            await asyncio.gather(*work, return_exceptions=True)
        for scope, ref in sources:
            snapshot = await self.repository.aread(scope)
            if any(job.source.ref == ref for job in snapshot.pending.values()):
                await self.app._notifications.put(
                    {
                        "type": "memory.deferred",
                        "thread_id": thread_id,
                        "scope": scope.kind,
                        "source_ref": ref,
                    }
                )

    async def resume_pending(self) -> None:
        """重启后恢复本工作区已持久化的来源，并补齐提交间隙。"""
        if not self.app.config.memory.enabled:
            return
        # 图 checkpoint 先于线程目录状态提交；遍历该分支全部终态检查点补齐间隙。
        offset = 0
        while True:
            page = await self.app.runtime.list_threads(
                workspace=self.app.workspace, limit=100, offset=offset
            )
            for thread in page:
                if thread.get("is_background"):
                    continue
                try:
                    handle, context = await self.app._context_for_thread(str(thread["thread_id"]))
                    if context.trust_level == "read_only" or not context.memory_learning_enabled:
                        continue
                    seen: set[str] = set()
                    async for snapshot in handle.graph.aget_state_history(
                        self.app.runtime.thread_config(context.session_id)
                    ):
                        marker = snapshot.values.get("memory_turn") if snapshot.values else None
                        if not isinstance(marker, Mapping):
                            continue
                        ref = str(marker.get("message_id") or "")
                        if not ref or ref in seen or snapshot.next or snapshot.interrupts:
                            continue
                        seen.add(ref)
                        await self._enqueue_snapshot(handle, context, snapshot, schedule=True)
                except (KeyError, RuntimeError, ValueError):
                    continue
            if len(page) < 100:
                break
            offset += len(page)
        for scope in self._scopes():
            if (await self.repository.aread(scope)).pending:
                self._schedule_scope(scope)

    async def drain(self, timeout: float) -> tuple[str, ...]:
        return await self.tasks.drain(timeout)


__all__ = ["MemoryLearningCoordinator"]
