"""本机 Web 宿主：共享图资源，按工作区装配产品适配，并管理运行任务。"""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl

from ..agent import AgentRuntime
from ..agent.events import action_requests
from ..application import SayacodeApp
from ..approvals import normalize_trust
from ..audit import _redact
from ..config import Config, ConfigRepository
from ..paths import AppPaths
from ..sessions import _workspace_key, stream_approval
from ..tasks import TaskManager, TaskRecord, WorktreeManager
from ..tasks import inbox as task_inbox_ops
from .attachments import AttachmentStore
from .events import EventHub
from .products import ProductOperations
from .run_actions import RunActions
from .session_operations import SessionOperations
from .views import activity_view, message_view, session_view, task_view, todo_view
from .workspaces import WorkspaceRegistry, workspace_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class _ActiveRun:
    run_id: str
    thread_id: str
    started_at: str
    control: RunControl
    task: asyncio.Task[None]


class WebHost(SessionOperations, ProductOperations, RunActions):
    """Web 请求只安排操作；浏览器断线不会取消正在执行的 Agent 图。"""

    def __init__(
        self,
        *,
        paths: AppPaths,
        repository: ConfigRepository,
        config: Config,
        runtime: AgentRuntime,
    ) -> None:
        self.paths = paths
        self.repository = repository
        self.config = config
        self.runtime = runtime
        self.workspaces = WorkspaceRegistry(runtime.store)
        self.attachments = AttachmentStore(paths.home)
        self.events = EventHub()
        self.tasks = TaskManager(runtime.store, WorktreeManager(paths.worktrees))
        self._apps: dict[str, SayacodeApp] = {}
        self._app_lock = asyncio.Lock()
        self._run_creation_locks: dict[str, asyncio.Lock] = {}
        self._runs: dict[str, _ActiveRun] = {}
        self._deleting_sessions: set[str] = set()
        self._stopping_sessions: set[str] = set()
        self._stopping_families: dict[str, set[str]] = {}
        self._closed = False
        self.initial_workspace_id: str | None = None

    @classmethod
    async def open(
        cls, initial_workspace: str | Path, *, home: str | Path | None = None
    ) -> WebHost:
        """无模型配置时也能启动页面；图与模型只在实际运行时创建。"""
        paths = AppPaths.resolve(home)
        repository = ConfigRepository(paths.home)
        config = await repository.load()
        runtime = await AgentRuntime.open(paths.home)
        host = cls(paths=paths, repository=repository, config=config, runtime=runtime)
        try:
            initial = await host.workspaces.register(initial_workspace)
            host.initial_workspace_id = str(initial["id"])
            # 跨工作区只协调一次孤儿任务，避免把本进程其他工作区误判为中断。
            await host.tasks.reconcile_orphans()
            host.tasks.on_update = host._on_task_update
            await host.attachments.cleanup_orphans()
            await host._app_for_workspace(host.initial_workspace_id)
            return host
        except BaseException:
            await host.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for run in self._runs.values():
            run.control.request_drain("Web service exit")
        if self._runs:
            _, pending = await asyncio.wait(
                [run.task for run in self._runs.values()], timeout=self._shutdown_grace_seconds()
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self.tasks.shutdown(timeout=self._shutdown_grace_seconds())
        for app in list(self._apps.values()):
            await app.aclose()
        await self.runtime.close()

    def _shutdown_grace_seconds(self) -> float:
        try:
            value = float(self.config.preferences.get("shutdown_grace_seconds", "10"))
        except ValueError:
            return 10.0
        return value if value > 0 else 10.0

    def _session_deletion_guard(self, thread_id: str) -> asyncio.Lock:
        """删除与新运行共用会话锁，避免检查后又启动任务。"""
        return self._run_creation_locks.setdefault(thread_id, asyncio.Lock())

    async def session_guard(self, thread_id: str) -> asyncio.Lock:
        app, _, _, _ = await self._thread_ref(thread_id)
        root = await app._root_thread_id(thread_id)
        return self._session_deletion_guard(root)

    async def _family_guard_for(self, thread_id: str) -> asyncio.Lock:
        return await self.session_guard(thread_id)

    async def _remove_thread_attachments(self, thread_id: str) -> None:
        await self.attachments.remove_thread(thread_id)

    def _set_session_deleting(self, thread_id: str, deleting: bool) -> None:
        if deleting:
            self._deleting_sessions.add(thread_id)
        else:
            self._deleting_sessions.discard(thread_id)

    def _active_session_threads(self, thread_ids: set[str]) -> set[str]:
        active = thread_ids.intersection(self._runs)
        for app in self._apps.values():
            active.update(thread_ids.intersection(app._wake_threads.values()))
        active.update(
            thread_ids.intersection(f"task-{task_id}" for task_id in self.tasks.active_task_ids())
        )
        return active

    def _invalidate_handles(self) -> None:
        for app in self._apps.values():
            app.profile_name = self.config.default_profile
            app.profile_override = None
            app._handles.clear()

    async def _reload_all_mcp(self) -> None:
        """用户级 MCP 变更会影响每个已载入工作区。"""
        for app in self._apps.values():
            await app.mcp.reload()
        self._invalidate_handles()

    async def _workspace(self, workspace_id: str) -> dict[str, Any]:
        return await self.workspaces.get(workspace_id)

    async def _app_for_workspace(self, workspace_id: str) -> SayacodeApp:
        """每个工作区只装配一套 MCP、Hook、Skill 和记忆资源。"""
        cached = self._apps.get(workspace_id)
        if cached is not None:
            return cached
        async with self._app_lock:
            cached = self._apps.get(workspace_id)
            if cached is not None:
                return cached
            row = await self._workspace(workspace_id)
            root = Path(str(row["path"])).resolve()
            active = await self.runtime.store.aget(("active_sessions",), _workspace_key(root))
            session_id = (
                str(active.value["thread_id"])
                if active is not None and active.value.get("thread_id")
                else f"session-{uuid4().hex[:12]}"
            )
            metadata = await self.runtime.get_thread(session_id)
            if metadata is not None and Path(str(metadata.get("workspace", ""))).resolve() != root:
                session_id = f"session-{uuid4().hex[:12]}"
            app = SayacodeApp(
                paths=self.paths,
                repository=self.repository,
                config=self.config,
                runtime=self.runtime,
                workspace=root,
                session_id=session_id,
                trust_level=self.config.default_trust,
                profile_name=self.config.default_profile,
                task_manager=self.tasks,
                owns_runtime=False,
                reconcile_tasks=False,
            )
            app._family_lock_for = self._session_deletion_guard
            try:
                await app.initialize()
            except BaseException:
                await app.aclose()
                raise
            app.watch_notifications(
                lambda event: self._forward_notification(workspace_id, event)
            )
            self._apps[workspace_id] = app
            return app

    async def _thread_ref(
        self, thread_id: str
    ) -> tuple[SayacodeApp, dict[str, Any], str, TaskRecord | None]:
        row = await self.runtime.get_thread(thread_id)
        record: TaskRecord | None
        if row is None and thread_id.startswith("task-"):
            record = await self.tasks.get(thread_id.removeprefix("task-"))
            if record.thread_id != thread_id:
                raise KeyError(f"未知线程：{thread_id}")
            row = {
                "thread_id": thread_id,
                "workspace": record.task_workspace or record.workspace,
                "task_id": record.task_id,
                "is_background": True,
                "title": record.title,
                "trust_level": record.trust_level,
                "status": record.status,
            }
        elif row is None:
            raise KeyError(f"未知线程：{thread_id}")
        task_id = row.get("task_id")
        record = await self.tasks.get(str(task_id)) if task_id else None
        root = Path(record.workspace if record else str(row["workspace"])).resolve()
        identity = workspace_id(root)
        await self._workspace(identity)
        return await self._app_for_workspace(identity), row, identity, record

    async def _app_for_thread(self, thread_id: str) -> SayacodeApp:
        app, _, _, _ = await self._thread_ref(thread_id)
        return app

    async def _publish_thread_change(
        self, thread_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        _, _, identity, record = await self._thread_ref(thread_id)
        await self.events.publish(
            event_type=event_type,
            workspace_id=identity,
            thread_id=thread_id,
            task_id=record.task_id if record else None,
            data=data,
        )

    async def _on_task_update(self, record: TaskRecord) -> None:
        identity = workspace_id(Path(record.workspace))
        app = await self._app_for_workspace(identity)
        await task_inbox_ops.on_task_update(app, record)
        if record.status == "idle" and not record.auto_wake_suspended:
            await app.task_inbox.promote_next(record.thread_id)

    async def _forward_notification(self, workspace_id: str, raw: Mapping[str, Any]) -> None:
        data = dict(raw)
        event_type = str(data.pop("type", "notification"))
        thread_id = data.pop("thread_id", None)
        task_id = data.pop("task_id", None)
        run_id = data.pop("run_id", None)
        await self.events.publish(
            event_type=event_type,
            workspace_id=workspace_id,
            thread_id=str(thread_id) if thread_id else None,
            task_id=str(task_id) if task_id else None,
            run_id=str(run_id) if run_id else None,
            data=_redact(data),
        )

    async def _publish_run_event(
        self, workspace_id: str, thread_id: str, run_id: str, raw: Mapping[str, Any]
    ) -> None:
        data = dict(raw)
        event_type = str(data.pop("type", "run.progress"))
        data.pop("thread_id", None)
        task_id = data.pop("task_id", None)
        await self.events.publish(
            event_type=event_type,
            workspace_id=workspace_id,
            thread_id=thread_id,
            task_id=str(task_id) if task_id else None,
            run_id=run_id,
            data=_redact(data),
        )

    async def status(self) -> dict[str, Any]:
        initial = self.initial_workspace_id
        app = await self._app_for_workspace(initial) if initial else None
        return {
            "model": app.model if app else None,
            "protocol": app.protocol if app else None,
            "trust_level": self.config.default_trust,
            "workspace_id": initial,
            "session_id": app.session_id if app else None,
            "running_tasks": len(self.tasks.active_task_ids()),
            "running_agents": (
                len(self._runs)
                + len(self.tasks.active_task_ids())
                + sum(len(item._wake_runs) for item in self._apps.values())
            ),
        }

    async def settings(self) -> dict[str, Any]:
        if self.initial_workspace_id is None:
            raise RuntimeError("尚无可用工作区")
        app = await self._app_for_workspace(self.initial_workspace_id)
        return {
            "language": self.config.preferences.get("language", "auto"),
            "default_trust": self.config.default_trust,
            "active_profile": self.config.default_profile,
            "memory_enabled": self.config.memory.enabled,
            "output_limit_bytes": app._output_limit_bytes(),
            "task_notice_limit_bytes": app._task_notice_limit_bytes(),
            "max_consecutive_wakes": app._max_consecutive_wakes(),
            "shutdown_grace_seconds": app._shutdown_grace_seconds(),
        }

    async def update_settings(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        numeric_keys = (
            "output_limit_bytes",
            "task_notice_limit_bytes",
            "max_consecutive_wakes",
            "shutdown_grace_seconds",
        )
        allowed = {
            "memory_enabled",
            "default_trust",
            "language",
            "active_profile",
            *numeric_keys,
        }
        if not patch or set(patch) - allowed:
            raise ValueError("设置字段为空或不受支持")
        selected = normalize_trust(str(patch["default_trust"])) if "default_trust" in patch else None
        language = str(patch["language"]) if "language" in patch else None
        if language is not None and language not in {"auto", "zh", "en"}:
            raise ValueError("未知语言")
        profile = str(patch["active_profile"]) if "active_profile" in patch else None
        if profile is not None:
            self.config.profile(profile)
        if "memory_enabled" in patch and not isinstance(patch["memory_enabled"], bool):
            raise ValueError("memory_enabled 必须为布尔值")
        for key in numeric_keys:
            if key not in patch:
                continue
            number = patch[key]
            if key == "shutdown_grace_seconds":
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(number)
                    or number <= 0
                ):
                    raise ValueError(f"{key} 必须为正数")
            elif isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                raise ValueError(f"{key} 必须为正整数")

        if "memory_enabled" in patch:
            if self.initial_workspace_id is None:
                raise RuntimeError("尚无可用工作区")
            app = await self._app_for_workspace(self.initial_workspace_id)
            await app.memory.settings({"enabled": patch["memory_enabled"]})
        if selected is not None:
            self.config.default_trust = selected
        if language is not None:
            self.config.preferences["language"] = language
        if profile is not None:
            self.config.default_profile = profile
        for key in numeric_keys:
            if key in patch:
                self.config.preferences[key] = str(patch[key])
        await self.repository.save(self.config)
        self._invalidate_handles()
        return await self.settings()

    async def list_workspaces(self) -> list[dict[str, Any]]:
        rows = await self.workspaces.list()
        result = []
        for row in rows:
            active = await self.runtime.store.aget(
                ("active_sessions",), _workspace_key(Path(str(row["path"])))
            )
            result.append(
                {
                    "id": row["id"],
                    "path": row["path"],
                    "name": row["name"],
                    "active_session_id": active.value.get("thread_id") if active else None,
                }
            )
        return result

    async def create_workspace(self, path: str, name: str | None) -> dict[str, Any]:
        row = await self.workspaces.register(path, name)
        await self._app_for_workspace(str(row["id"]))
        return next(
            item for item in await self.list_workspaces() if item["id"] == row["id"]
        )

    async def rename_workspace(self, workspace_id: str, name: str) -> dict[str, Any]:
        await self.workspaces.rename(workspace_id, name)
        return next(
            item for item in await self.list_workspaces() if item["id"] == workspace_id
        )

    async def list_sessions(self, workspace_id: str) -> list[dict[str, Any]]:
        root = Path(str((await self._workspace(workspace_id))["path"]))
        rows = await self.runtime.list_threads(workspace=root, limit=500)
        return [
            session_view(row, workspace_id)
            for row in rows
            if not row.get("is_background")
        ]

    async def create_session(self, workspace_id: str, title: str | None) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        thread_id = await app._new_session(title)
        row = await self.runtime.get_thread(thread_id)
        assert row is not None
        return session_view(row, workspace_id)

    async def rename_thread(self, thread_id: str, title: str) -> dict[str, Any]:
        _, row, identity, _ = await self._thread_ref(thread_id)
        if row.get("is_background"):
            raise ValueError("子任务标题请从任务面板管理")
        selected = title.strip()
        if not selected:
            raise ValueError("会话标题不能为空")
        updated = await self.runtime.update_thread(thread_id, {"title": selected})
        return session_view(updated, identity)

    async def set_trust(self, thread_id: str, trust_level: str) -> dict[str, str]:
        async with await self._family_guard_for(thread_id):
            app, _, _, record = await self._thread_ref(thread_id)
            chosen = normalize_trust(trust_level)
            async with app._thread_lock(thread_id):
                if thread_id in self._active_session_threads({thread_id}):
                    raise RuntimeError("运行中的线程不能切换信任档，请先停止或等待")
                await app._save_thread_policy(thread_id, trust_level=chosen)
                if record is not None and record.trust_level != chosen:
                    record.trust_level = chosen
                    await self.tasks.update(record)
                app._handles.clear()
                return {"trust_level": chosen}

    async def _state(
        self, app: SayacodeApp, thread_id: str
    ) -> tuple[dict[str, Any], list[Any], str, bool]:
        """读取官方图快照；模型未配置时仍能读取原生 checkpoint 中的历史。"""
        try:
            handle, _ = await app._context_for_thread(thread_id)
            snapshot = await self.runtime.get_state(handle, thread_id)
            config = snapshot.config.get("configurable", {})
            return (
                dict(snapshot.values or {}),
                list(snapshot.interrupts),
                str(config.get("checkpoint_id") or ""),
                bool(snapshot.next),
            )
        except (KeyError, ValueError, RuntimeError):
            saved = await self.runtime.checkpointer.aget_tuple(
                cast(RunnableConfig, self.runtime.thread_config(thread_id))
            )
            if saved is None:
                return {}, [], "", False
            checkpoint = saved.checkpoint
            config = saved.config.get("configurable", {})
            return (
                dict(checkpoint.get("channel_values", {})),
                [],
                str(config.get("checkpoint_id") or ""),
                False,
            )

    async def thread_snapshot(self, thread_id: str) -> dict[str, Any]:
        app, row, identity, record = await self._thread_ref(thread_id)
        values, interrupts, checkpoint_id, has_next = await self._state(app, thread_id)
        messages = list(values.get("messages") or [])
        call_candidates: dict[str, list[Mapping[str, Any]]] = {}
        for item in messages:
            for call in getattr(item, "tool_calls", []) or []:
                if isinstance(call, Mapping) and call.get("id"):
                    call_candidates.setdefault(str(call["id"]), []).append(call)
        result_candidates: dict[str, list[Any]] = {}
        for item in messages:
            call_id = getattr(item, "tool_call_id", None)
            if getattr(item, "type", "") == "tool" and call_id:
                result_candidates.setdefault(str(call_id), []).append(item)
        todos = list(values.get("todos") or [])
        actions = action_requests(interrupts)
        records = await self.tasks.list(workspace=app.workspace)
        activity_rows = await app.audit.list(thread_id=thread_id, limit=500)
        audit_call_counts = Counter(
            str((item.get("details") or {}).get("tool_call_id"))
            for item in activity_rows
            if item.get("event") == "tool.started"
            and isinstance(item.get("details"), dict)
            and item["details"].get("tool_call_id")
        )
        tool_calls = {
            call_id: candidates[0]
            for call_id, candidates in call_candidates.items()
            if len(candidates) == 1 and audit_call_counts[call_id] <= 1
        }
        tool_results = {
            call_id: candidates[0]
            for call_id, candidates in result_candidates.items()
            if len(candidates) == 1 and audit_call_counts[call_id] <= 1
        }
        run = self._runs.get(thread_id)
        wake = next(
            (
                (message_id, app._wake_started_at.get(message_id, ""))
                for message_id, receiver in app._wake_threads.items()
                if receiver == thread_id and not app._wake_runs[message_id].done()
            ),
            None,
        )
        explicit_model = row.get("profile_override_name")
        inherited_model = (
            (record.profile_snapshot or {}).get("name") or record.profile_name
            if record is not None
            else row.get("profile_name") or self.config.default_profile
        )
        effective_model = explicit_model or inherited_model
        queued_messages = await self.queued_messages(thread_id)
        pending_inbox = await app.task_inbox.pending(thread_id)
        return {
            "thread_id": thread_id,
            "workspace_id": identity,
            "title": row.get("title") or ("子任务" if row.get("is_background") else "新会话"),
            "status": (
                "stopping"
                if row.get("auto_wake_suspended") is True
                and self._active_session_threads(
                    self._stopping_families.get(thread_id, {thread_id})
                )
                else "stopped"
                if row.get("auto_wake_suspended") is True
                else "running"
                if run or wake
                else "paused"
                if actions
                else record.status
                if record is not None
                else row.get("status") or "idle"
            ),
            "trust_level": (
                record.trust_level
                if record is not None and await self.runtime.get_thread(thread_id) is None
                else (await app._load_thread_policy(thread_id)).trust_level
            ),
            "effective_model": effective_model,
            "model_source": "thread" if explicit_model else "task" if record else "default",
            "profile_override_name": explicit_model,
            "queued_messages": queued_messages,
            "resume_available": (
                has_next
                or bool(queued_messages)
                or bool(pending_inbox)
                or (record is None and row.get("auto_wake_suspended") is True)
            ) and not bool(interrupts),
            "pending_steps": has_next,
            "messages": [
                message_view(item, index, tool_calls) for index, item in enumerate(messages)
            ],
            "todos": [todo_view(item, index) for index, item in enumerate(todos)],
            "pending_approval": (
                {"checkpoint_id": checkpoint_id, "actions": actions} if actions else None
            ),
            "tasks": [
                task_view(record, identity)
                for record in records
                if record.parent_thread_id == thread_id
            ],
            "activity": [activity_view(item, tool_calls, tool_results) for item in activity_rows],
            "active_run": (
                {
                    "run_id": run.run_id,
                    "started_at": run.started_at,
                    "status": "running",
                }
                if run
                else {
                    "run_id": f"wake-{wake[0]}",
                    "started_at": wake[1],
                    "status": "running",
                    "source": "inbox",
                }
                if wake
                else None
            ),
        }

    async def set_thread_model(self, thread_id: str, name: str) -> dict[str, Any]:
        """为当前线程选择模型，从下一次运行开始生效。"""
        app, _, _, _ = await self._thread_ref(thread_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("请选择已配置的模型")
        selected = name.strip()
        app.config.profile(selected)
        await self.runtime.update_thread(thread_id, {"profile_override_name": selected})
        return await self.thread_snapshot(thread_id)

    async def list_thread_tasks(self, thread_id: str) -> list[dict[str, Any]]:
        app, _, identity, _ = await self._thread_ref(thread_id)
        return [
            task_view(record, identity)
            for record in await self.tasks.list(workspace=app.workspace)
            if record.parent_thread_id == thread_id
        ]

    async def start_run(self, thread_id: str, message: str) -> dict[str, str]:
        async with self._run_creation_locks.setdefault(thread_id, asyncio.Lock()):
            return await self._start_run_locked(thread_id, message)

    async def resume_run(self, thread_id: str) -> dict[str, str]:
        """只续跑已保存的超步，不把新消息插到未完成工具调用中间。"""
        async with self._session_deletion_guard(thread_id):
            app, row, identity, record = await self._thread_ref(thread_id)
            if record is not None or row.get("is_background"):
                raise ValueError("子 Agent 请使用任务恢复")
            if thread_id in self._deleting_sessions or thread_id in self._runs:
                raise ValueError("此会话当前不可恢复")
            if row.get("auto_wake_suspended") is True and self._active_session_threads(
                self._stopping_families.get(thread_id, {thread_id})
            ):
                raise ValueError("会话及子 Agent 尚在停止，请等待停止完成")
            handle, _ = await app._context_for_thread(thread_id)
            snapshot = await self.runtime.get_state(handle, thread_id)
            if snapshot.interrupts:
                raise ValueError("此会话有待批准操作，请先处理审批")
            if not snapshot.next:
                pending = await app.task_inbox.pending(thread_id)
                queued = await app.task_inbox.queued(thread_id)
                if not pending and not queued and row.get("auto_wake_suspended") is not True:
                    raise ValueError("此会话没有待继续的执行或排队消息")
                self._stopping_sessions.discard(thread_id)
                self._stopping_families.pop(thread_id, None)
                app._stopping_threads.discard(thread_id)
                app._wake_counts.pop(thread_id, None)
                await self.runtime.update_thread(
                    thread_id, {"auto_wake_suspended": False, "status": "idle"}
                )
                if not pending and not queued:
                    await self.events.publish(
                        event_type="thread.resumed",
                        workspace_id=identity,
                        thread_id=thread_id,
                        data={"status": "idle"},
                    )
                    return {"run_id": "", "thread_id": thread_id, "status": "idle"}
                if pending:
                    selected_message = pending[0]
                    task_inbox_ops.schedule_wake(app, selected_message)
                else:
                    queued_message = await app.task_inbox.promote_next(thread_id)
                    if queued_message is None:
                        raise ValueError("排队消息已变化，请刷新后重试")
                    selected_message = queued_message
                return {
                    "run_id": f"wake-{selected_message.message_id}",
                    "thread_id": thread_id,
                    "status": "running",
                }
            self._stopping_sessions.discard(thread_id)
            self._stopping_families.pop(thread_id, None)
            app._stopping_threads.discard(thread_id)
            await self.runtime.update_thread(thread_id, {"auto_wake_suspended": False})
            run_id = uuid4().hex
            control = RunControl()
            await self.events.publish(
                event_type="run.started", workspace_id=identity, thread_id=thread_id,
                run_id=run_id, data={"source": "resume"},
            )
            task = asyncio.create_task(
                self._execute_run(app, identity, thread_id, run_id, None, control),
                name=f"sayacode-web-resume-{run_id}",
            )
            self._runs[thread_id] = _ActiveRun(run_id, thread_id, _now(), control, task)
            return {"run_id": run_id, "thread_id": thread_id, "status": "running"}

    async def _start_run_locked(self, thread_id: str, message: str) -> dict[str, str]:
        app, row, identity, _ = await self._thread_ref(thread_id)
        if thread_id in self._deleting_sessions:
            raise ValueError("会话正在删除")
        try:
            await app._effective_profile(thread_id)
        except (KeyError, RuntimeError) as exc:
            raise ValueError("请先在模型设置中配置并选用模型") from exc
        if row.get("is_background"):
            raise ValueError("子 Agent 请使用任务追问")
        if thread_id in self._runs:
            raise ValueError("此会话已有正在执行的请求")
        _, interrupts, _, has_next = await self._state(app, thread_id)
        if interrupts:
            raise ValueError("此会话有待批准操作，请先处理审批")
        if has_next:
            raise ValueError("此会话有尚未完成的执行，请先继续原运行")
        selected = message.strip()
        if not selected:
            raise ValueError("消息不能为空")
        self._stopping_sessions.discard(thread_id)
        self._stopping_families.pop(thread_id, None)
        app._stopping_threads.discard(thread_id)
        await self.runtime.update_thread(thread_id, {"auto_wake_suspended": False})
        run_id = uuid4().hex
        control = RunControl()
        await self.events.publish(
            event_type="message.user",
            workspace_id=identity,
            thread_id=thread_id,
            run_id=run_id,
            data={"text": selected},
        )
        await self.events.publish(
            event_type="run.started",
            workspace_id=identity,
            thread_id=thread_id,
            run_id=run_id,
            data={"source": "user"},
        )
        task = asyncio.create_task(
            self._execute_run(app, identity, thread_id, run_id, selected, control),
            name=f"sayacode-web-run-{run_id}",
        )
        self._runs[thread_id] = _ActiveRun(run_id, thread_id, _now(), control, task)
        return {"run_id": run_id, "thread_id": thread_id, "status": "running"}

    async def _execute_run(
        self,
        app: SayacodeApp,
        workspace_id: str,
        thread_id: str,
        run_id: str,
        message: str | None,
        control: RunControl,
    ) -> None:
        terminal: str | None = None
        try:
            async for event in app.stream(
                message,
                session_id=thread_id,
                include_notifications=False,
                control=control,
            ):
                await self._publish_run_event(workspace_id, thread_id, run_id, event)
                if event.get("type") in {
                    "run.completed", "run.paused", "run.failed", "run.stopped"
                }:
                    terminal = str(event["type"])
        except asyncio.CancelledError:
            await self.events.publish(
                event_type="run.interrupted",
                workspace_id=workspace_id,
                thread_id=thread_id,
                run_id=run_id,
                data={"reason": "process shutting down"},
            )
            raise
        except Exception as exc:
            await self.events.publish(
                event_type="run.failed",
                workspace_id=workspace_id,
                thread_id=thread_id,
                run_id=run_id,
                data={"error": str(exc)},
            )
        finally:
            self._runs.pop(thread_id, None)
            if terminal == "run.completed" and thread_id not in self._stopping_sessions:
                await app.task_inbox.promote_next(thread_id)

    async def decide_approval(
        self,
        thread_id: str,
        checkpoint_id: str,
        decisions: list[dict[str, str]],
        grants: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        # 审批恢复和整棵任务树停止必须由同一把主会话锁串行化。
        async with await self._family_guard_for(thread_id):
            return await self._decide_approval_locked(
                thread_id, checkpoint_id, decisions, grants or []
            )

    async def _decide_approval_locked(
        self,
        thread_id: str,
        checkpoint_id: str,
        decisions: list[dict[str, str]],
        grants: list[dict[str, Any]],
    ) -> dict[str, str]:
        app, _, identity, record = await self._thread_ref(thread_id)
        if thread_id in self._deleting_sessions:
            raise ValueError("会话正在删除")
        if record is not None:
            root_id = await app._root_thread_id(thread_id)
            root = await self.runtime.get_thread(root_id)
            if (
                root_id in self._deleting_sessions
                or root_id in self._stopping_sessions
                or root is None
                or root.get("auto_wake_suspended") is True
                or root.get("status") in {"stopping", "stopped"}
            ):
                raise ValueError("所属主会话已停止，请先恢复主会话")
        if thread_id in self._runs:
            raise ValueError("此会话仍在执行")
        _, interrupts, current_id, _ = await self._state(app, thread_id)
        if not interrupts or current_id != checkpoint_id:
            raise ValueError("审批快照已变化，请刷新后重试")
        actions = action_requests(interrupts)
        if len(actions) != len(decisions):
            raise ValueError("审批决定与待批准操作数量不一致")
        if grants and (await app._load_thread_policy(thread_id)).trust_level != "ask":
            raise ValueError("只有询问档可以记住已批准调用")
        for grant in grants:
            index = grant.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(actions)
                or decisions[index].get("type") != "approve"
                or grant.get("tool_name") != actions[index].get("name")
            ):
                raise ValueError("记住授权与待批准调用不匹配")
        run_id = uuid4().hex
        self._stopping_sessions.discard(thread_id)
        self._stopping_families.pop(thread_id, None)
        app._stopping_threads.discard(thread_id)
        await self.runtime.update_thread(thread_id, {"auto_wake_suspended": False})
        await self.events.publish(
            event_type="run.started",
            workspace_id=identity,
            thread_id=thread_id,
            run_id=run_id,
            data={"source": "approval"},
        )
        control = RunControl()
        task = asyncio.create_task(
            self._execute_approval(
                app, identity, thread_id, run_id, decisions, grants, control
            ),
            name=f"sayacode-web-approval-{run_id}",
        )
        self._runs[thread_id] = _ActiveRun(run_id, thread_id, _now(), control, task)
        return {"run_id": run_id, "thread_id": thread_id, "status": "running"}

    async def _execute_approval(
        self,
        app: SayacodeApp,
        workspace_id: str,
        thread_id: str,
        run_id: str,
        decisions: list[dict[str, str]],
        grants: list[dict[str, Any]],
        control: RunControl,
    ) -> None:
        terminal: str | None = None
        try:
            async for event in stream_approval(
                app, thread_id, decisions, grants=grants, control=control
            ):
                await self._publish_run_event(workspace_id, thread_id, run_id, event)
                if event.get("type") in {
                    "run.completed", "run.paused", "run.failed", "run.stopped"
                }:
                    terminal = str(event["type"])
        except asyncio.CancelledError:
            raise
        except GraphDrained:
            await self._publish_run_event(
                workspace_id, thread_id, run_id, {"type": "run.stopped", "ok": False}
            )
        except Exception as exc:
            await self._publish_run_event(
                workspace_id, thread_id, run_id, {"type": "run.failed", "error": str(exc)}
            )
        finally:
            self._runs.pop(thread_id, None)
            if terminal == "run.completed" and thread_id not in self._stopping_sessions:
                await app.task_inbox.promote_next(thread_id)

    async def list_tasks(self, workspace_id: str | None) -> list[dict[str, Any]]:
        root = (
            Path(str((await self._workspace(workspace_id))["path"])) if workspace_id else None
        )
        return [
            task_view(record, workspace_id or workspace_id_for_record(record))
            for record in await self.tasks.list(workspace=root)
        ]

    async def spawn_task(
        self,
        parent_thread_id: str,
        role: str,
        prompt: str,
        title: str | None,
        worktree_enabled: bool | None,
    ) -> dict[str, Any]:
        app, _, identity, _ = await self._thread_ref(parent_thread_id)
        root = await app._root_thread_id(parent_thread_id)
        if root in self._deleting_sessions or root in self._stopping_sessions:
            raise ValueError("主会话正在停止或删除")
        record = await app._spawn_task(
            prompt,
            role=role,
            parent_thread_id=parent_thread_id,
            title=title,
            use_worktree=worktree_enabled,
        )
        return task_view(record, identity)

    async def task_action(
        self, task_id: str, action: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        record = await self.tasks.get(task_id)
        identity = workspace_id(Path(record.workspace))
        app = await self._app_for_workspace(identity)
        if action == "wait":
            record = await self.tasks.wait(task_id)
        elif action == "stop":
            async with await self._family_guard_for(record.thread_id):
                record = await self.tasks.stop(task_id)
        elif action == "resume":
            async with await self._family_guard_for(record.thread_id):
                root_id = await app._root_thread_id(record.thread_id)
                root = await self.runtime.get_thread(root_id)
                if root_id in self._deleting_sessions or root_id in self._stopping_sessions:
                    raise ValueError("所属主会话正在停止或删除")
                if root is not None and root.get("auto_wake_suspended") is True:
                    raise ValueError("请先恢复所属主会话")
                record = await self.tasks.get(task_id)
                if record.status == "paused":
                    raise ValueError("此子任务正在等待审批")
                if record.status == "failed":
                    raise ValueError("失败的子任务需要新指令，请使用追问")
                record = await self.tasks.resume(task_id, app._task_runner)
                app._stopping_threads.discard(record.thread_id)
                await app.task_inbox.promote_next(record.thread_id)
        elif action == "followup":
            message = str(payload.get("message") or "").strip()
            if not message:
                raise ValueError("追问内容不能为空")
            if record.status == "paused":
                raise ValueError("请先处理子任务的待批操作")
            await self.queue_message(record.thread_id, message)
            record = await self.tasks.get(task_id)
        elif action == "diff":
            delivery = await self.tasks.delivery(task_id)
            return {"task": task_view(record, identity), "diff": delivery.get("patch", "")}
        elif action == "apply":
            async with await self._family_guard_for(record.thread_id):
                delivery = await self.tasks.apply_delivery(task_id)
                record = await self.tasks.get(task_id)
            return {
                "task": task_view(record, identity),
                "status": "applied" if delivery.get("applied") else "unchanged",
                "message": str(delivery.get("reason") or ""),
            }
        elif action == "cleanup":
            async with await self._family_guard_for(record.thread_id):
                record = await self.tasks.remove_worktree(task_id)
        else:
            raise ValueError(f"未知任务操作：{action}")
        return {"task": task_view(record, identity), "status": record.status}

    async def subscribe(
        self, workspace_id: str | None, after: int | None, instance_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        if workspace_id is not None:
            await self._workspace(workspace_id)
        async for event in self.events.subscribe(workspace_id, after, instance_id):
            yield event


def workspace_id_for_record(record: TaskRecord) -> str:
    return workspace_id(Path(record.workspace))


__all__ = ["WebHost"]
