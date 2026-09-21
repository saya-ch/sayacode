"""应用组装入口。围绕官方框架做产品适配。不自建循环也不另存副本。装配顺序是路径配置运行时加会话，智能体句柄按档案加信任加工具表缓存复用。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from langchain_core.tools import BaseTool
from langgraph.runtime import RunControl

from . import diagnostics, profiles, sessions
from .agent import AgentContext, AgentHandle, AgentRuntime
from .agent.events import EventProjector, _final_text, action_requests
from .agent.graph import TaskNotificationMiddleware
from .agent.models import _model_error_message
from .audit import AuditLog, LangChainAuditCallback
from .config import Config, ConfigRepository, Profile
from .extensions import memory as memory_ops
from .extensions.hooks import HookMiddleware, HookResult, HookRuntime
from .extensions.mcp import MCPOutputMiddleware, MCPRegistry
from .extensions.memory import load_project_instructions
from .paths import AppPaths
from .prompts import (
    PromptPreferences,
    build_system_prompt,
    normalize_language,
    normalize_style,
)
from .sessions import _now, _workspace_key
from .tasks import TaskManager, TaskRecord, WorktreeManager
from .tasks import coordinator as task_coordinator
from .tools import build_tools, tool_catalog
from .trust import READ_TOOLS, Policy, PolicyMiddleware, build_approval_middleware, normalize_trust


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
        self.audit = AuditLog(paths.audit)
        self._thread_policies: dict[str, Policy] = {}
        self.hooks = HookRuntime(
            self.workspace,
            state_home=paths.home,
            audit=self._audit_hook,
        )
        self.tasks = TaskManager(
            runtime.store,
            WorktreeManager(paths.worktrees),
            on_update=self._on_task_update,
        )
        self._handles: dict[tuple[str, str, str], AgentHandle] = {}
        self.mcp = MCPRegistry(self.workspace, self.config, self._save_config, self._handles.clear)
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.events = EventProjector()
        self._notification_watchers: set[asyncio.Task[None]] = set()
        self._spawned_task_ids: set[str] = set()
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._wake_runs: dict[str, asyncio.Task[None]] = {}
        self._wake_controls: dict[str, RunControl] = {}
        self._wake_results: dict[str, dict[str, Any]] = {}
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
                saved["trust_level"] = self.trust_level
                saved["updated_at"] = _now()
                await self.runtime.store.aput(("threads",), self.session_id, saved, index=False)
        await self.tasks.reconcile_orphans()
        await self._set_active_session(self.session_id)
        await self._ensure_thread(self.session_id, self.trust_level)
        await self.mcp.reload()
        await self.hooks.trigger("SessionStart", {"thread_id": self.session_id})
        await self._recover_parent_wakes(self.session_id)
        return self

    async def aclose(self) -> None:
        """按序关闭任务唤醒钩子和运行资源。传入无。多次调用只执行一次，退出时先让后台任务收尾再关监听。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self.tasks.shutdown(timeout=self._shutdown_grace_seconds())
            for control in self._wake_controls.values():
                control.request_drain("CLI exit")
            if self._wake_runs:
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

    def _audit_callback(self, thread_id: str, task_id: str | None = None) -> LangChainAuditCallback:
        return LangChainAuditCallback(self.audit, thread_id=thread_id, task_id=task_id)

    async def _on_task_update(self, record: TaskRecord) -> None:
        return await task_coordinator._on_task_update(self, record)

    def _thread_lock(self, thread_id: str) -> asyncio.Lock:
        return task_coordinator._thread_lock(self, thread_id)

    def _schedule_parent_wake(self, event_id: str) -> None:
        return task_coordinator._schedule_parent_wake(self, event_id)

    async def _parent_event_items(
        self, parent_thread_id: str, *, state: str | None = None
    ) -> list[Any]:
        return await task_coordinator._parent_event_items(self, parent_thread_id, state=state)

    async def _schedule_pending_wakes(self, parent_thread_id: str) -> None:
        return await task_coordinator._schedule_pending_wakes(self, parent_thread_id)

    async def _recover_parent_wakes(self, parent_thread_id: str) -> None:
        return await task_coordinator._recover_parent_wakes(self, parent_thread_id)

    async def _set_parent_event_state(
        self, event: dict[str, Any], state: str, **details: Any
    ) -> None:
        return await task_coordinator._set_parent_event_state(self, event, state, **details)

    async def _acknowledge_task_event(self, record: TaskRecord, parent_thread_id: str) -> None:
        return await task_coordinator._acknowledge_task_event(self, record, parent_thread_id)

    @staticmethod
    def _task_notice(event: dict[str, Any]) -> str:
        return task_coordinator._task_notice(event)

    async def _wake_parent(self, event_id: str, control: RunControl) -> None:
        return await task_coordinator._wake_parent(self, event_id, control)

    async def next_notification(self) -> dict[str, Any]:
        """取下一条后台任务通知。"""
        return await task_coordinator.next_notification(self)

    def watch_notifications(self, callback: Any) -> asyncio.Task[None]:
        """订阅后台任务通知，有新通知就回调。"""
        return task_coordinator.watch_notifications(self, callback)

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

    async def _save_thread_policy(self, thread_id: str) -> None:
        return await sessions._save_thread_policy(self, thread_id)

    def _context(
        self,
        thread_id: str,
        trust_level: str,
        *,
        workspace: Path | None = None,
        task_id: str | None = None,
        background: bool = False,
        profile_name: str | None = None,
    ) -> AgentContext:
        return sessions._context(
            self,
            thread_id,
            trust_level,
            workspace=workspace,
            task_id=task_id,
            background=background,
            profile_name=profile_name,
        )

    def _output_limit_bytes(self) -> int:
        try:
            value = int(self.config.preferences.get("output_limit_bytes", "65536"))
        except ValueError:
            return 64 * 1024
        return value if value > 0 else 64 * 1024

    def _tools_for_context(
        self, context: AgentContext, *, include_team_tools: bool = True
    ) -> list[BaseTool]:
        items = build_tools(context)
        if include_team_tools:
            items.extend(self._team_tools())
        if context.trust_level == "read_only":
            allowed = READ_TOOLS | {"execute_command_tool", "delegate_to_subagent"}
            items = [item for item in items if item.name in allowed]
        return items

    def _shutdown_grace_seconds(self) -> float:
        try:
            value = float(self.config.preferences.get("shutdown_grace_seconds", "10"))
        except ValueError:
            return 10.0
        return value if value > 0 else 10.0

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
        background: bool = False,
        include_team_tools: bool = True,
        profile_override: Profile | None = None,
    ) -> tuple[AgentHandle, AgentContext]:
        """取或建智能体句柄。大函数分四段看，先按会话恢复策略拼上下文，再按档位过滤工具集，接着按档案加工作区加工具表算缓存键，命中直接返回，未命中组提示词和中间件新建。只读档不装外部服务工具，换档换档案要清缓存才生效。"""
        await self._load_thread_policy(thread_id, trust_level=trust_level)
        context = self._context(
            thread_id,
            trust_level,
            workspace=workspace,
            task_id=task_id,
            background=background,
            profile_name=profile_override.name if profile_override else None,
        )
        profile = profile_override or self._profile()
        explicit_tools = self._tools_for_context(context, include_team_tools=include_team_tools)
        mcp_tools = (
            []
            if context.trust_level == "read_only"
            else await self.mcp.tools_for_workspace(context.workspace)
        )
        all_tools = [*explicit_tools, *mcp_tools]
        key = (
            hashlib.sha256(repr(asdict(profile)).encode("utf-8")).hexdigest(),
            trust_level,
            f"{context.workspace}|{task_id or ''}|{','.join(tool.name for tool in all_tools)}",
        )
        handle = self._handles.get(key)
        if handle is None:
            instructions = load_project_instructions(context.workspace, self.paths.memory)
            prefs = PromptPreferences(
                style=self.config.preferences.get("style", "standard"),
                language=self.config.preferences.get("language", "auto"),
            )
            prompt = build_system_prompt(
                str(context.workspace), prefs, project_instructions=instructions
            )
            approval = build_approval_middleware(all_tools)
            handle = self.runtime.build_agent(
                profile,
                explicit_tools,
                context=context,
                system_prompt=prompt,
                model_override=self.model_override,
                additional_tools=mcp_tools,
                interrupt_on=approval.interrupt_on,
                extra_middleware=[
                    TaskNotificationMiddleware(),
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
                    PolicyMiddleware(),
                    MCPOutputMiddleware(self.paths.outputs, limit=context.output_limit_bytes),
                ],
            )
            self._handles[key] = handle
        return handle, context

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        input_format: str = "interactive",
    ) -> dict[str, Any]:
        """非流式跑一轮用户输入。传入提示词和会话号，返回完成暂停或失败字典。先拿会话锁防并发，结束后顺手调度父唤醒。"""
        thread_id = session_id or self.session_id
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
        active_trust = (await self._load_thread_policy(thread_id)).trust_level
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
            return {"ok": True, "status": "completed", "thread_id": thread_id, "response": response}
        except Exception as exc:
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
    ) -> AsyncIterator[dict[str, Any]]:
        """流式跑一轮用户输入。传入提示词和会话号，逐个吐出投影后的事件。同样先拿会话锁，流尽后调度父唤醒，中途异常包成失败事件。"""
        thread_id = session_id or self.session_id
        async with self._thread_lock(thread_id):
            async for event in self._stream_unlocked(
                prompt, session_id=thread_id, input_format=input_format
            ):
                yield event
        await self._schedule_pending_wakes(thread_id)

    async def _stream_unlocked(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        input_format: str = "interactive",
    ) -> AsyncIterator[dict[str, Any]]:
        """跑一轮并输出精简稳定的原生事件。"""
        thread_id = session_id or self.session_id
        active_trust = (await self._load_thread_policy(thread_id)).trust_level
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
                callbacks=[self._audit_callback(thread_id)],
            )
            final: dict[str, Any] | None = None
            async with run:
                async for event in run:
                    for public in self.events.normalize(event, thread_id):
                        yield public
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
                yield {
                    "type": "run.completed",
                    "thread_id": thread_id,
                    "response": response,
                    "ok": True,
                }
        except Exception as exc:
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
        return task_coordinator._team_tools(self)

    async def _spawn_task(
        self,
        prompt: str,
        *,
        role: str,
        parent_thread_id: str | None,
        profile_name: str | None = None,
    ) -> TaskRecord:
        return await task_coordinator._spawn_task(
            self, prompt, role=role, parent_thread_id=parent_thread_id, profile_name=profile_name
        )

    async def _task_runner(self, record: TaskRecord, control: RunControl) -> str | None:
        return await task_coordinator._task_runner(self, record, control)

    async def wait_for_tasks(self) -> list[dict[str, Any]]:
        """等全部后台任务落定。传入无，返回任务结果表。调用方退出前用它收尾，不要在持有会话锁时调。"""
        return await task_coordinator.wait_for_tasks(self)

    async def command(self, name: str, args: Any = "") -> Any:
        """终端用的命令入口。保持薄适配。传入命令名和参数，返回各命令自定结果。名字大小写和斜杠都先抹平，未知命令抛错。审批类转交会话恢复，档案模型类转交档案函数。"""
        command = name.lower().strip().lstrip("/")
        if command in {"approve", "reject"}:
            return await self._resume_approval(command, args)
        if command in {"status", "stats", "context"}:
            return await self._status()
        if command == "workspace":
            return str(self.workspace)
        if command == "paths":
            return {
                key: str(getattr(self.paths, key))
                for key in (
                    "home",
                    "config",
                    "checkpoints",
                    "store",
                    "audit",
                    "outputs",
                    "worktrees",
                )
            }
        if command in {"sessions", "session"}:
            return await self._session_command(args)
        if command == "history":
            return await self._history()
        if command == "compact":
            handle, context = await self._get_handle(
                thread_id=self.session_id, trust_level=self.trust_level
            )
            return {
                "compacted": await self.runtime.compact(
                    handle,
                    context,
                    thread_id=self.session_id,
                    focus=str(args).strip() or None,
                )
            }
        if command == "rewind":
            return await self._rewind(args)
        if command == "reset":
            return {"session_id": await self._new_session()}
        if command == "trust":
            return await self._trust_command(args)
        if command == "model" and str(args or "").strip() in self.config.profiles:
            return await self._config_command(f"use {str(args).strip()}")
        if command in {"config", "model"}:
            return await self._config_command(args)
        if command == "mcp":
            return await self.mcp.command(args)
        if command == "tools":
            context = self._context(self.session_id, self.trust_level)
            catalog = tool_catalog(
                [
                    *self._tools_for_context(context),
                    *([] if context.trust_level == "read_only" else self.mcp.tools),
                ]
            )
            requested = str(args or "").strip()
            if requested:
                return next(
                    (item for item in catalog if item["name"] == requested),
                    {"ok": False, "error": f"Unknown tool: {requested}"},
                )
            return catalog
        if command == "todos":
            return await self._todos()
        if command == "team":
            return await self._team_command(args)
        if command == "trace":
            rows = await self.audit.list(thread_id=self.session_id)
            requested = str(args or "").strip()
            return (
                [
                    row
                    for row in rows
                    if row.get("run_id") == requested
                    or row.get("details", {}).get("parent_run_id") == requested
                ]
                if requested
                else rows
            )
        if command == "doctor":
            return await self._doctor(args)
        if command == "git":
            return await self._git_command(args)
        if command == "analyze":
            return await self._invoke_native_tool("analyze_project")
        if command == "symbols":
            query = str(args or "").strip()
            return await self._invoke_native_tool("list_symbols", query=query)
        if command == "hooks":
            return self.hooks.status()
        if command == "memory":
            return await self._memory_command(args)
        if command == "lang":
            self.config.preferences["language"] = normalize_language(str(args or "auto"))
            self._handles.clear()
            await self._save_config()
            return {"language": self.config.preferences["language"]}
        if command == "style":
            self.config.preferences["style"] = normalize_style(str(args or "standard"))
            self._handles.clear()
            await self._save_config()
            return {"style": self.config.preferences["style"]}
        if command == "prefs":
            return dict(self.config.preferences)
        if command == "settings":
            return await self._settings_command(args)
        if command == "commands":
            return {"ok": True}
        raise NotImplementedError(f"Unknown command: {name}")

    async def _memory_command(self, args: Any) -> Any:
        return await memory_ops._memory_command(self, args)

    async def _settings_command(self, args: Any) -> dict[str, Any]:
        return await profiles._settings_command(self, args)

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
        return await task_coordinator._task_by_thread(self, thread_id)

    async def _status(self) -> dict[str, Any]:
        return await diagnostics._status(self)

    async def _session_command(self, args: Any) -> Any:
        return await sessions._session_command(self, args)

    async def _history(self) -> list[dict[str, Any]]:
        return await sessions._history(self)

    async def _rewind(self, args: Any) -> Any:
        return await sessions._rewind(self, args)

    async def _config_command(self, args: Any) -> Any:
        return await profiles._config_command(self, args)

    async def _save_config(self) -> None:
        return await profiles._save_config(self)

    async def _trust_command(self, args: Any) -> dict[str, Any]:
        return await sessions._trust_command(self, args)

    async def _todos(self) -> Any:
        return await sessions._todos(self)

    async def _team_command(self, args: Any) -> Any:
        return await task_coordinator._team_command(self, args)

    async def _doctor(self, bundle: Any = "") -> dict[str, Any]:
        return await diagnostics._doctor(self, bundle)

    async def _git_command(self, args: Any) -> Any:
        return await diagnostics._git_command(self, args)

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
    )
    try:
        return await app.initialize()
    except BaseException:
        await runtime.close()
        raise


__all__ = ["SayacodeApp", "create_app"]
