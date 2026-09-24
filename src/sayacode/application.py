"""应用组装入口。围绕官方框架做产品适配。不自建循环也不另存副本。装配顺序是路径配置运行时加会话，智能体句柄按档案加信任加工具表缓存复用。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, AsyncIterator, Sequence
from uuid import uuid4

from langchain_core.tools import BaseTool
from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl

from . import diagnostics, profiles, sessions
from .agent import AgentContext, AgentHandle, AgentRuntime
from .agent.events import EventProjector, _final_text, action_requests
from .agent.models import _model_error_message
from .approvals import (
    READ_TOOLS,
    JevReviewer,
    JevReviewMiddleware,
    Policy,
    PolicyMiddleware,
    build_approval_middleware,
    normalize_trust,
)
from .audit import AuditLog, LangChainAuditCallback
from .config import Config, ConfigRepository, Profile
from .extensions.hooks import HookMiddleware, HookResult, HookRuntime
from .extensions.instructions import load_project_instructions
from .extensions.mcp import MCPOutputMiddleware, MCPRegistry
from .extensions.skills import (
    SkillActivation,
    SkillRegistry,
    SkillsMiddleware,
    skill_budget_bytes,
    skill_tools,
    validate_skill_budget,
)
from .memory.middleware import MemoryRetrievalMiddleware
from .memory.service import MemoryService, model_identity_sha256
from .memory.tools import memory_tools
from .memory.turns import MemoryTurnMiddleware
from .paths import AppPaths
from .prompts import (
    AgentRole,
    PromptPreferences,
    build_system_prompt,
)
from .sessions import _workspace_key
from .tasks import TaskInbox, TaskInboxMiddleware, TaskManager, TaskRecord, WorktreeManager
from .tasks import inbox as task_inbox_ops
from .tasks import manager as task_manager_ops
from .tasks.tools import child_tools, parent_tools
from .tools import build_tools


class SayacodeApp:
    """围绕单个运行时收拢用户可见命令。重逻辑在会话档案任务三组函数里，这里只做装配和转发。"""

    def __init__(
        self,
        *,
        paths: AppPaths,
        repository: ConfigRepository,
        config: Config,
        runtime: AgentRuntime,
        workspace: Path,
        session_id: str,
        trust_level: str,
        trust_explicit: bool = False,
        profile_name: str | None,
        profile_override: Profile | None = None,
        model_override: Any = None,
        headless: bool = False,
        task_manager: TaskManager | None = None,
        owns_runtime: bool = True,
        reconcile_tasks: bool = True,
    ) -> None:
        self.paths = paths
        self.repository = repository
        self.config = config
        self.runtime = runtime
        self.workspace = workspace.resolve()
        self.session_id = session_id
        self.trust_level = normalize_trust(trust_level)
        self.trust_explicit = trust_explicit
        self.profile_name = profile_name
        self.profile_override = profile_override
        self.model_override = model_override
        self.headless = headless
        self._owns_runtime = owns_runtime
        self._owns_tasks = task_manager is None
        self._reconcile_tasks = reconcile_tasks
        self.audit = AuditLog(paths.audit)
        self._thread_policies: dict[str, Policy] = {}
        self.hooks = HookRuntime(
            self.workspace,
            state_home=paths.home,
            audit=self._audit_hook,
        )
        self.tasks = task_manager or TaskManager(
            runtime.store,
            WorktreeManager(paths.worktrees),
            on_update=self._on_task_update,
        )
        self.task_inbox = TaskInbox(
            runtime.store, on_send=lambda message: task_inbox_ops.schedule_wake(self, message)
        )
        self._handles: dict[tuple[str, str, str], AgentHandle] = {}
        self.mcp = MCPRegistry(self.workspace, self.config, self._save_config, self._handles.clear)
        self.skills = SkillRegistry(paths.home / "skills", self.workspace)
        self.memory = MemoryService(self)
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.events = EventProjector()
        self._notification_watchers: set[asyncio.Task[None]] = set()
        self._spawned_task_ids: set[str] = set()
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._wake_runs: dict[str, asyncio.Task[None]] = {}
        self._wake_controls: dict[str, RunControl] = {}
        self._wake_results: dict[str, dict[str, Any]] = {}
        self._wake_counts: dict[str, int] = {}
        self._closed = False

    @property
    def model(self) -> str | None:
        """返回当前模型实例。"""
        try:
            return self._profile().model_id
        except (RuntimeError, KeyError):
            return None

    @property
    def protocol(self) -> str | None:
        """返回当前模型的协议名。"""
        try:
            return self._profile().protocol
        except (RuntimeError, KeyError):
            return None

    async def initialize(self) -> "SayacodeApp":
        """完成启动收尾并返回自身。传入无。校验会话归属工作区，拉平信任档，恢复孤儿任务和父唤醒。会话串到别的工作区会直接抛错。"""
        saved = await self.runtime.get_thread(self.session_id)
        if saved is not None:
            if Path(saved.get("workspace", "")).resolve() != self.workspace:
                raise ValueError("Session belongs to another workspace")
            if not self.trust_explicit:
                self.trust_level = normalize_trust(saved.get("trust_level"))
            elif saved.get("trust_level") != self.trust_level:
                await self.runtime.update_thread(
                    self.session_id, {"trust_level": self.trust_level}
                )
        if self.trust_level == "jev" and self.config.jev is None and self.headless:
            raise ValueError("尚未配置 Jev 审理模型；请在 WebUI 设置中配置")
        if self._reconcile_tasks:
            await self.tasks.reconcile_orphans()
        await self._set_active_session(self.session_id)
        await self._ensure_thread(self.session_id, self.trust_level)
        await self.mcp.reload()
        await self.hooks.trigger("SessionStart", {"thread_id": self.session_id})
        if not self.headless and self.config.memory.enabled:
            await self.memory.resume_pending()
        await task_inbox_ops.schedule_pending(self, self.session_id)
        return self

    async def aclose(self) -> None:
        """按序关闭任务唤醒钩子和运行资源。传入无。多次调用只执行一次，退出时先让后台任务收尾再关监听。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._owns_tasks:
                await self.tasks.shutdown(timeout=self._shutdown_grace_seconds())
            await self.memory.drain(timeout=self._shutdown_grace_seconds())
            if self._wake_runs:
                for control in self._wake_controls.values():
                    control.request_drain("Web service exit")
                _, pending = await asyncio.wait(
                    list(self._wake_runs.values()), timeout=self._shutdown_grace_seconds()
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            await self.hooks.trigger("SessionEnd", {"thread_id": self.session_id})
        finally:
            watchers = list(self._notification_watchers)
            for watcher in watchers:
                watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            await self.mcp.close()
            if self._owns_runtime:
                await self.runtime.close()

    async def _audit_hook(
        self, result: HookResult, *, thread_id: str | None = None, task_id: str | None = None
    ) -> None:
        await self.audit.append(
            "hook",
            thread_id=thread_id or self.session_id,
            task_id=task_id,
            details={
                "event": result.event,
                "name": result.name,
                "source": result.source,
                "returncode": result.returncode,
                "blocked": result.blocked,
                "stdout_characters": len(result.stdout),
                "stderr_characters": len(result.stderr),
            },
        )

    async def _record_jev_reviews(self, reviews: list[dict[str, Any]], runtime: Any) -> None:
        """把 Jev 审理结果写入审计并推送公开进度事件。"""
        context = runtime.context
        thread_id = str(getattr(context, "session_id", self.session_id))
        task_id = getattr(context, "task_id", None)
        for review in reviews:
            public = {
                "type": "review.decision",
                "thread_id": thread_id,
                "task_id": task_id,
                "tool_name": review.get("tool_name"),
                "tool_call_id": review.get("tool_call_id"),
                "action": review.get("action"),
                "confidence": review.get("confidence"),
                "model": review.get("model"),
                "request_id": review.get("request_id"),
                "reason": review.get("reason"),
            }
            await self.audit.append(
                "review.decision", thread_id=thread_id, task_id=task_id, details=public
            )
            await self._notifications.put(public)

    def _audit_callback(self, thread_id: str, task_id: str | None = None) -> LangChainAuditCallback:
        loop = asyncio.get_running_loop()

        def emit(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(self._notifications.put_nowait, event)

        return LangChainAuditCallback(
            self.audit,
            thread_id=thread_id,
            task_id=task_id,
            on_model_event=emit,
        )

    async def _on_task_update(self, record: TaskRecord) -> None:
        return await task_inbox_ops.on_task_update(self, record)

    def _thread_lock(self, thread_id: str) -> asyncio.Lock:
        return self._thread_locks.setdefault(thread_id, asyncio.Lock())

    async def _schedule_pending_wakes(self, parent_thread_id: str) -> None:
        return await task_inbox_ops.schedule_pending(self, parent_thread_id)

    async def next_notification(self) -> dict[str, Any]:
        """取下一条后台任务通知。"""
        return await self._notifications.get()

    def drain_notifications(self) -> list[dict[str, Any]]:
        """取空当前进程已产生的公开通知，供无流式 JSONL 补齐事件。"""
        items: list[dict[str, Any]] = []
        while not self._notifications.empty():
            items.append(self._notifications.get_nowait())
        return items

    def watch_notifications(self, callback: Any) -> asyncio.Task[None]:
        """订阅后台任务通知，有新通知就回调。"""

        async def watch() -> None:
            while True:
                event = await self.next_notification()
                result = callback(event)
                if hasattr(result, "__await__"):
                    await result

        task = asyncio.create_task(watch(), name="sayacode-notifications")
        self._notification_watchers.add(task)
        task.add_done_callback(self._notification_watchers.discard)
        return task

    def _profile(self) -> Profile:
        return profiles._profile(self)

    async def _ensure_thread(self, thread_id: str, trust_level: str) -> None:
        return await sessions._ensure_thread(self, thread_id, trust_level)

    def _policy_for_thread(self, thread_id: str, trust_level: str | None = None) -> Policy:
        return sessions._policy_for_thread(self, thread_id, trust_level)

    async def _load_thread_policy(
        self, thread_id: str, *, trust_level: str | None = None
    ) -> Policy:
        return await sessions._load_thread_policy(self, thread_id, trust_level=trust_level)

    async def _save_thread_policy(
        self,
        thread_id: str,
        *,
        trust_level: str | None = None,
        add_grants: Sequence[str] = (),
        clear_grants: bool = False,
    ) -> None:
        await sessions._save_thread_policy(
            self,
            thread_id,
            trust_level=trust_level,
            add_grants=add_grants,
            clear_grants=clear_grants,
        )

    def _context(
        self,
        thread_id: str,
        trust_level: str,
        *,
        workspace: Path | None = None,
        task_id: str | None = None,
        agent_role: AgentRole = "main",
        background: bool = False,
        profile_name: str | None = None,
    ) -> AgentContext:
        return sessions._context(
            self,
            thread_id,
            trust_level,
            workspace=workspace,
            task_id=task_id,
            agent_role=agent_role,
            background=background,
            profile_name=profile_name,
        )

    def _output_limit_bytes(self) -> int:
        try:
            value = int(self.config.preferences.get("output_limit_bytes", "65536"))
        except ValueError:
            return 64 * 1024
        return value if value > 0 else 64 * 1024

    def _task_notice_limit_bytes(self) -> int:
        """返回自动注入父上下文的单条子 Agent 结果字节上限。"""
        try:
            value = int(self.config.preferences.get("task_notice_limit_bytes", "16384"))
        except ValueError:
            return 16 * 1024
        return value if value > 0 else 16 * 1024

    def _tools_for_context(
        self,
        context: AgentContext,
        *,
        include_team_tools: bool = True,
        profile: Profile | None = None,
    ) -> list[BaseTool]:
        items = build_tools(context)
        budget = skill_budget_bytes(profile) if profile is not None else 16 * 1024
        items.extend(skill_tools(self.skills, max_active_bytes=budget))
        if self.config.memory.enabled:
            available_memory_tools = memory_tools(self)
            if context.memory_use_enabled:
                items.append(available_memory_tools[0])
            if context.memory_learning_mode == "explicit":
                items.append(available_memory_tools[1])
        if include_team_tools:
            items.extend(parent_tools(self))
        elif context.task_id is not None:
            items.extend(child_tools(self))
        if context.trust_level == "read_only":
            allowed = READ_TOOLS | {"delegate_to_subagent"}
            items = [item for item in items if item.name in allowed]
        return items

    def _shutdown_grace_seconds(self) -> float:
        try:
            value = float(self.config.preferences.get("shutdown_grace_seconds", "10"))
        except ValueError:
            return 10.0
        return value if value > 0 else 10.0

    def _max_consecutive_wakes(self) -> int:
        """返回同一父线程由后台消息连续唤醒的轮次上限。"""
        try:
            value = int(self.config.preferences.get("max_consecutive_wakes", "3"))
        except ValueError:
            return 3
        return value if value > 0 else 3

    async def _set_active_session(self, session_id: str) -> None:
        return await sessions._set_active_session(self, session_id)

    async def _new_session(self, title: str | None = None) -> str:
        return await sessions._new_session(self, title)

    async def _get_handle(
        self,
        *,
        thread_id: str,
        trust_level: str,
        workspace: Path | None = None,
        task_id: str | None = None,
        agent_role: AgentRole = "main",
        background: bool = False,
        include_team_tools: bool = True,
        profile_override: Profile | None = None,
    ) -> tuple[AgentHandle, AgentContext]:
        """按画像、角色、工作区和工具集复用或创建编译图。

        只读线程不加载 MCP。角色进入系统提示和缓存键，确保主 Agent 与三类子
        Agent 不会共用错误的角色提示。配置或信任变化由调用方清理缓存后生效。
        """
        # 其他 CLI 可能刚关闭全局记忆；新一轮建上下文前先读最新安装配置。
        if self.repository.path.is_file():
            await self.repository.refresh(self.config)
        await self._load_thread_policy(thread_id, trust_level=trust_level)
        context = self._context(
            thread_id,
            trust_level,
            workspace=workspace,
            task_id=task_id,
            agent_role=agent_role,
            background=background,
            profile_name=profile_override.name if profile_override else None,
        )
        session_memory = await self.memory.session_settings(thread_id)
        context = replace(
            context,
            memory_use_enabled=bool(session_memory["use"]),
            memory_learning_mode=str(session_memory["learn"]),
            memory_learning_enabled=session_memory["learn"] == "auto",
        )
        profile = profile_override or self._profile()
        if context.memory_learning_enabled:
            memory_profile = (
                self.config.profile(self.config.memory.model_profile)
                if self.config.memory.model_profile
                else profile
            )
            context = replace(
                context,
                memory_profile_name=memory_profile.name,
                memory_model_identity_sha256=model_identity_sha256(memory_profile),
            )
        explicit_tools = self._tools_for_context(
            context, include_team_tools=include_team_tools, profile=profile
        )
        mcp_tools = (
            []
            if context.trust_level == "read_only"
            else await self.mcp.tools_for_workspace(context.workspace)
        )
        all_tools = [*explicit_tools, *mcp_tools]
        instructions = load_project_instructions(context.workspace, self.paths.instructions)
        instructions_sha256 = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
        key = (
            hashlib.sha256(repr(asdict(profile)).encode("utf-8")).hexdigest(),
            trust_level,
            f"{context.workspace}|{task_id or ''}|{agent_role}|{instructions_sha256}|{','.join(tool.name for tool in all_tools)}",
        )
        handle = self._handles.get(key)
        if handle is None:
            prefs = PromptPreferences(
                language=self.config.preferences.get("language", "auto"),
            )
            prompt = build_system_prompt(
                str(context.workspace),
                prefs,
                project_instructions=instructions,
                role=context.agent_role,
            )
            approval = build_approval_middleware(all_tools)
            reviewer_middleware: list[Any] = []
            if context.trust_level == "jev":
                if self.config.jev is None:
                    raise ValueError("尚未配置 Jev 审理模型；请在 WebUI 设置中配置")
                reviewer_middleware.append(
                    JevReviewMiddleware(
                        JevReviewer(self.config.jev), on_reviews=self._record_jev_reviews
                    )
                )
            handle = self.runtime.build_agent(
                profile,
                explicit_tools,
                context=context,
                system_prompt=prompt,
                model_override=self.model_override,
                additional_tools=mcp_tools,
                interrupt_on=approval.interrupt_on,
                extra_middleware=[
                    MemoryTurnMiddleware(),
                    MemoryRetrievalMiddleware(
                        self.memory.repository,
                        lambda: self.config.memory,
                        profile,
                        on_retrieved=self.memory.record_references,
                    ),
                    SkillsMiddleware(self.skills, max_active_bytes=skill_budget_bytes(profile)),
                    TaskInboxMiddleware(
                        self.task_inbox.pending,
                        self.task_inbox.acknowledge,
                    ),
                    HookMiddleware(
                        self.hooks
                        if context.workspace == self.workspace
                        else HookRuntime(
                            context.workspace,
                            state_home=self.paths.home,
                            audit=lambda result: self._audit_hook(
                                result, thread_id=thread_id, task_id=task_id
                            ),
                            trust_origin=self.workspace,
                        )
                    ),
                    *reviewer_middleware,
                    PolicyMiddleware(),
                    MCPOutputMiddleware(self.paths.outputs, limit=context.output_limit_bytes),
                ],
            )
            self._handles[key] = handle
        return handle, context

    async def activate_skill(self, name: str, *, thread_id: str | None = None) -> SkillActivation:
        """显式激活 Skill，把正文写入当前线程的原生检查点。"""
        tid = thread_id or self.session_id
        async with self._thread_lock(tid):
            thread = await self.runtime.get_thread(tid)
            if thread is None:
                raise KeyError(f"未找到会话：{tid}")
            workspace = Path(str(thread["workspace"])).resolve()
            activation = await asyncio.to_thread(self.skills.activate, name, workspace)
            trust = normalize_trust(thread.get("trust_level"))
            thread_profile = str(thread.get("profile_name") or "")
            profile = (
                self.profile_override
                if self.profile_override is not None
                and self.profile_override.name == thread_profile
                else self.config.profiles.get(thread_profile) or self._profile()
            )
            handle, _ = await self._get_handle(
                thread_id=tid,
                trust_level=trust,
                workspace=workspace,
                task_id=thread.get("task_id"),
                agent_role=thread.get("agent_role", "main"),
                background=bool(thread.get("is_background", False)),
                include_team_tools=thread.get("task_id") is None,
                profile_override=profile,
            )
            snapshot = await self.runtime.get_state(handle, tid)
            if snapshot.interrupts or (
                snapshot.next and snapshot.values and snapshot.values.get("messages")
            ):
                raise RuntimeError("当前会话尚有待完成的执行或审批，暂不能激活 Skill")
            current = snapshot.values.get("active_skills", {}) if snapshot.values else {}
            validate_skill_budget(current, activation, skill_budget_bytes(profile))
            await handle.graph.aupdate_state(
                self.runtime.thread_config(tid),
                {"active_skills": {activation.name: activation.content}},
                as_node="__start__",
            )
            return activation

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        input_format: str = "interactive",
    ) -> dict[str, Any]:
        """非流式跑一轮用户输入。传入提示词和会话号，返回完成暂停或失败字典。先拿会话锁防并发，结束后顺手调度父唤醒。"""
        thread_id = session_id or self.session_id
        self._wake_counts.pop(thread_id, None)
        async with self._thread_lock(thread_id):
            outcome = await self._run_unlocked(
                prompt, session_id=thread_id, input_format=input_format
            )
        await self._schedule_pending_wakes(thread_id)
        return outcome

    async def _run_unlocked(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        input_format: str = "interactive",
    ) -> dict[str, Any]:
        """跑一次非流式用户轮次。走官方图执行。"""
        thread_id = session_id or self.session_id
        self.memory.begin_turn(thread_id)
        active_trust = (await self._load_thread_policy(thread_id)).trust_level
        handle: AgentHandle | None = None
        context: AgentContext | None = None
        try:
            block = await self.hooks.trigger(
                "UserPromptSubmit", {"prompt": prompt, "thread_id": thread_id}
            )
            if block:
                return {"ok": False, "status": "failed", "error": block, "thread_id": thread_id}
            handle, context = await self._get_handle(thread_id=thread_id, trust_level=active_trust)
            result = await self.runtime.invoke(
                handle,
                context,
                prompt,
                thread_id=thread_id,
                callbacks=[self._audit_callback(thread_id)],
            )
            if result.interrupts:
                await self.audit.append(
                    "run.paused", thread_id=thread_id, details={"interrupts": result.interrupts}
                )
                return {
                    "ok": False,
                    "status": "paused",
                    "thread_id": thread_id,
                    "interrupts": result.interrupts,
                }
            response = _final_text(result)
            await self.audit.append(
                "run.completed", thread_id=thread_id, details={"response_chars": len(response)}
            )
            await self._finalize_memory(handle, context, headless=input_format == "headless")
            return {"ok": True, "status": "completed", "thread_id": thread_id, "response": response}
        except GraphDrained:
            await self.audit.append("run.stopped", thread_id=thread_id)
            return {"ok": False, "status": "stopped", "thread_id": thread_id}
        except Exception as exc:
            if handle is not None and context is not None:
                await self._finalize_memory(
                    handle, context, headless=input_format == "headless", failed=True
                )
            try:
                profile = self._profile()
            except (KeyError, ValueError):
                profile = None
            error = _model_error_message(exc, profile)
            await self.audit.append("run.failed", thread_id=thread_id, details={"error": error})
            return {"ok": False, "status": "failed", "thread_id": thread_id, "error": error}

    async def stream(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        input_format: str = "interactive",
        include_notifications: bool = True,
        control: RunControl | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式跑一轮用户输入。传入提示词和会话号，逐个吐出投影后的事件。同样先拿会话锁，流尽后调度父唤醒，中途异常包成失败事件。"""
        thread_id = session_id or self.session_id
        self._wake_counts.pop(thread_id, None)
        async with self._thread_lock(thread_id):
            async for event in self._stream_unlocked(
                prompt,
                session_id=thread_id,
                input_format=input_format,
                include_notifications=include_notifications,
                control=control,
            ):
                yield event
        await self._schedule_pending_wakes(thread_id)

    async def _stream_unlocked(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        input_format: str = "interactive",
        include_notifications: bool = True,
        control: RunControl | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """跑一轮并输出精简稳定的原生事件。"""
        thread_id = session_id or self.session_id
        self.memory.begin_turn(thread_id)
        active_trust = (await self._load_thread_policy(thread_id)).trust_level
        handle: AgentHandle | None = None
        context: AgentContext | None = None
        try:
            block = await self.hooks.trigger(
                "UserPromptSubmit", {"prompt": prompt, "thread_id": thread_id}
            )
            if block:
                yield {"type": "run.failed", "thread_id": thread_id, "error": block}
                return
            handle, context = await self._get_handle(thread_id=thread_id, trust_level=active_trust)
            run = await self.runtime.open_event_stream_v3(
                handle,
                context,
                prompt,
                thread_id=thread_id,
                control=control,
                callbacks=[self._audit_callback(thread_id)],
            )
            final: dict[str, Any] | None = None
            async with run:
                async for event in run:
                    for public in self.events.normalize(event, thread_id):
                        yield public
                    if include_notifications:
                        while not self._notifications.empty():
                            yield self._notifications.get_nowait()
                interrupted = await run.interrupted()
                final = await run.output()
                interrupts = await run.interrupts()
            if interrupted:
                await self.runtime.set_thread_status(thread_id, "interrupted")
                await self.audit.append(
                    "run.paused", thread_id=thread_id, details={"interrupts": interrupts}
                )
                yield {
                    "type": "approval.requested",
                    "thread_id": thread_id,
                    "action_requests": action_requests(interrupts),
                    "trust_level": context.trust_level,
                }
                yield {"type": "run.paused", "thread_id": thread_id}
            else:
                await self.runtime.set_thread_status(thread_id, "completed")
                response = _final_text(final or {})
                await self.audit.append(
                    "run.completed", thread_id=thread_id, details={"response_chars": len(response)}
                )
                await self._finalize_memory(handle, context, headless=input_format == "headless")
                yield {
                    "type": "run.completed",
                    "thread_id": thread_id,
                    "response": response,
                    "ok": True,
                }
        except GraphDrained:
            await self.audit.append("run.stopped", thread_id=thread_id)
            yield {"type": "run.stopped", "thread_id": thread_id, "ok": False}
        except Exception as exc:
            if handle is not None and context is not None:
                await self._finalize_memory(
                    handle, context, headless=input_format == "headless", failed=True
                )
            try:
                profile = self._profile()
            except (KeyError, ValueError):
                profile = None
            error = _model_error_message(exc, profile)
            await self.audit.append("run.failed", thread_id=thread_id, details={"error": error})
            yield {"type": "run.failed", "thread_id": thread_id, "error": error, "ok": False}

    def _normalize_event(self, event: dict[str, Any], thread_id: str) -> list[dict[str, Any]]:
        """供诊断调用使用同一事件投影器。"""
        return self.events.normalize(event, thread_id)

    def _team_tools(self) -> list[BaseTool]:
        return parent_tools(self)

    async def _spawn_task(
        self,
        prompt: str,
        *,
        role: str,
        parent_thread_id: str | None,
        profile_name: str | None = None,
        context_snapshot: dict[str, Any] | None = None,
        use_worktree: bool | None = None,
        title: str | None = None,
    ) -> TaskRecord:
        if self._closed:
            raise RuntimeError("CLI is closing; cannot start a background task")
        if role not in {"builder", "planner", "reviewer"}:
            raise ValueError("role must be builder, planner, or reviewer")
        parent_policy = (
            await self._load_thread_policy(parent_thread_id)
            if parent_thread_id
            else self._policy_for_thread(self.session_id)
        )
        worktree_enabled = role == "builder" if use_worktree is None else use_worktree
        if role != "builder":
            worktree_enabled = False
        record = await self.tasks.spawn(
            parent_thread_id=parent_thread_id,
            role=role,
            prompt=prompt,
            workspace=self.workspace,
            worktree_enabled=worktree_enabled,
            title=title,
            runner=self._task_runner,
            profile_name=profile_name or self.profile_name,
            trust_level=parent_policy.trust_level,
            profile_snapshot=asdict(self._profile()),
            context_snapshot=context_snapshot,
        )
        self._spawned_task_ids.add(record.task_id)
        return record

    async def _task_runner(self, record: TaskRecord, control: RunControl) -> str | None:
        return await task_manager_ops.run_task(self, record, control)

    async def wait_for_tasks(self) -> list[dict[str, Any]]:
        """等全部后台任务落定。传入无，返回任务结果表。调用方退出前用它收尾，不要在持有会话锁时调。"""
        return await task_manager_ops.wait_for_tasks(self)

    async def _finalize_memory(
        self,
        handle: AgentHandle,
        context: AgentContext,
        *,
        headless: bool = False,
        failed: bool = False,
    ) -> None:
        """整理故障只影响记忆状态，不改写已经成功的主任务结果。"""
        try:
            if failed:
                await self.memory.on_turn_failed(handle, context, schedule=not headless)
            else:
                await self.memory.on_turn_complete(handle, context, schedule=not headless)
        except Exception as exc:
            await self.audit.append(
                "memory.failed",
                thread_id=context.session_id,
                details={"error_type": type(exc).__name__},
            )
            await self._notifications.put(
                {
                    "type": "memory.failed",
                    "thread_id": context.session_id,
                    "error_type": type(exc).__name__,
                }
            )

    async def _context_for_thread(self, thread_id: str) -> tuple[AgentHandle, AgentContext]:
        return await sessions._context_for_thread(self, thread_id)

    async def pending_approval(self, thread_id: str | None = None) -> dict[str, Any]:
        """查看当前会话的待审批中断。传入会话号或空，返回状态和待批动作。只看不改，真正拍板走审批命令。"""
        return await sessions.pending_approval(self, thread_id)

    async def _resume_approval(self, command: str, args: Any) -> dict[str, Any]:
        return await sessions._resume_approval(self, command, args)

    async def _resume_approval_unlocked(
        self, command: str, args: dict[str, Any], thread_id: str
    ) -> dict[str, Any]:
        return await sessions._resume_approval_unlocked(self, command, args, thread_id)

    async def _task_by_thread(self, thread_id: str) -> TaskRecord | None:
        return await task_manager_ops.task_by_thread(self, thread_id)

    async def _status(self) -> dict[str, Any]:
        return await diagnostics._status(self)

    async def _history(self) -> list[dict[str, Any]]:
        return await sessions._history(self)

    async def _save_config(self) -> None:
        return await profiles._save_config(self)

    async def _doctor(self, bundle: Any = "") -> dict[str, Any]:
        return await diagnostics._doctor(self, bundle)

    async def _invoke_native_tool(self, tool_name: str, **arguments: Any) -> Any:
        return await diagnostics._invoke_native_tool(self, tool_name, **arguments)


async def create_app(args: Any) -> SayacodeApp:
    """从命令行参数创建默认本地应用。传入参数对象，返回初始化好的应用。分四段看，先解路径配置和工作区，再处理单次模型覆盖，接着选会话号，最后装配并初始化，初始化失败会关掉运行时再抛。单次模型字段要成套给，缺一个就抛错。"""
    paths = AppPaths.resolve()
    repository = ConfigRepository(paths.home)
    config = await repository.load()
    workspace = Path(args.workspace).expanduser().resolve()
    profile_override: Profile | None = None
    profile_name = getattr(args, "profile", None) or config.default_profile
    required = ("protocol", "base_url", "model_id", "context_length", "max_output_tokens")
    supplied = [key for key in (*required, "api_key") if getattr(args, key, None) is not None]
    no_api_key = bool(getattr(args, "no_api_key", False))
    if no_api_key:
        supplied.append("no_api_key")
    if supplied:
        if getattr(args, "profile", None):
            raise ValueError("--profile cannot be combined with one-run model endpoint fields")
        missing = [key for key in required if getattr(args, key, None) is None]
        if missing:
            raise ValueError("Incomplete model endpoint: missing " + ", ".join(missing))
        if no_api_key and getattr(args, "api_key", None) is not None:
            raise ValueError("--api-key and --no-api-key cannot be combined")
        if not no_api_key and not getattr(args, "api_key", None):
            raise ValueError("Incomplete model endpoint: missing --api-key (or --no-api-key)")
        profile_override = Profile(
            name="command-line",
            protocol=str(args.protocol),
            base_url=str(args.base_url),
            api_key=None if no_api_key else str(args.api_key),
            model_id=str(args.model_id),
            context_length=int(args.context_length),
            max_output_tokens=int(args.max_output_tokens),
        )
        profile_name = profile_override.name
    runtime = await AgentRuntime.open(paths.home)
    if getattr(args, "new_session", False):
        session_id = f"session-{uuid4().hex[:12]}"
    elif getattr(args, "session", None):
        session_id = str(args.session)
    else:
        item = await runtime.store.aget(("active_sessions",), _workspace_key(workspace))
        session_id = (
            str(item.value.get("thread_id")) if item is not None else f"session-{uuid4().hex[:12]}"
        )
    app = SayacodeApp(
        paths=paths,
        repository=repository,
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id=session_id,
        trust_level=getattr(args, "trust", None) or config.default_trust,
        trust_explicit=bool(getattr(args, "trust", None)),
        profile_name=profile_name,
        profile_override=profile_override,
        headless=getattr(args, "prompt", None) is not None,
    )
    try:
        return await app.initialize()
    except BaseException:
        await runtime.close()
        raise


__all__ = ["SayacodeApp", "create_app"]
