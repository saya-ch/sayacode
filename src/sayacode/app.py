"""SAYACODE 2.0 application composition.

This is deliberately a product adapter around LangChain and LangGraph.  It
does not run an agent loop or retain a parallel transcript.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Literal, cast
from uuid import uuid4

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl

from .audit import AuditLog, LangChainAuditCallback
from .config import Config, ConfigRepository, Profile
from .hooks import HookMiddleware, HookResult, HookRuntime
from .memory import append_user_memory, load_project_instructions
from .paths import AppPaths
from .policy import Action, Policy, PolicyMiddleware, build_approval_middleware
from .prompts import (
    PromptPreferences,
    build_system_prompt,
    normalize_language,
    normalize_mode,
    normalize_style,
)
from .runtime import AgentContext, AgentHandle, AgentRuntime
from .tasks import TaskError, TaskManager, TaskPaused, TaskRecord, WorktreeManager
from .tools import build_tools, namespace_mcp_tools, tool_catalog


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _workspace_key(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:24]


def _policy_rules(raw: dict[str, str]) -> dict[str, Action]:
    return {
        key: cast(Action, value) for key, value in raw.items() if value in {"allow", "ask", "deny"}
    }


def _final_text(state: Any) -> str:
    values = getattr(state, "value", state)
    if isinstance(values, dict):
        messages = values.get("messages", [])
        parts: list[str] = []
        for message in reversed(messages):
            if isinstance(message, AIMessage) and not message.tool_calls:
                parts.append(message.text if hasattr(message, "text") else str(message.content))
                continue
            if (
                parts and isinstance(message, HumanMessage)
                and message.additional_kwargs.get("sayacode_continuation")
            ):
                continue
            if parts:
                return "".join(reversed(parts))
            if isinstance(message, dict) and message.get("type") in {"ai", "assistant"}:
                content = message.get("content", "")
                return content if isinstance(content, str) else str(content)
        if parts:
            return "".join(reversed(parts))
    return ""


def _message_text(message: Any) -> str:
    value = getattr(message, "content", message)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item) for item in value
        )
    return str(value or "")


def _new_profile_name(model_id: str, existing: dict[str, Profile]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", model_id.lower()).strip("-")[:48] or "model"
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


class MCPOutputMiddleware(AgentMiddleware):
    """Keep large text MCP results in files while preserving native messages."""

    def __init__(self, output_dir: Path, limit: int = 64 * 1024) -> None:
        super().__init__()
        self.output_dir = output_dir
        self.limit = limit

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        result = await handler(request)
        if not str(request.tool_call.get("name") or "").startswith("mcp__"):
            return result
        if not isinstance(result, ToolMessage):
            return result
        content = await self._spill_value(result.content)
        artifact = await self._spill_value(result.artifact)
        return result.model_copy(update={"content": content, "artifact": artifact})

    async def _spill_value(self, value: Any) -> Any:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) <= self.limit:
                return value
            target = self.output_dir / f"mcp-{hashlib.sha256(encoded).hexdigest()}.txt"

            def write() -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(encoded)

            await asyncio.to_thread(write)
            preview = encoded[:self.limit].decode("utf-8", "ignore")
            return f"{preview}\n[Full MCP output: {target}; {len(encoded)} bytes]"
        if isinstance(value, list):
            return [await self._spill_value(item) for item in value]
        if isinstance(value, dict):
            if value.get("type") in {"image", "image_url", "audio", "audio_url"}:
                return value
            return {key: await self._spill_value(item) for key, item in value.items()}
        return value


class SayacodeApp:
    """Coordinates user-visible commands around a single LangGraph runtime."""

    def __init__(
        self,
        *,
        paths: AppPaths,
        repository: ConfigRepository,
        config: Config,
        runtime: AgentRuntime,
        workspace: Path,
        session_id: str,
        mode: str,
        mode_explicit: bool = False,
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
        self.mode = normalize_mode(mode)
        self.mode_explicit = mode_explicit
        self.profile_name = profile_name
        self.profile_override = profile_override
        self.model_override = model_override
        self.audit = AuditLog(paths.audit)
        self.policy = Policy(
            user=_policy_rules(config.user_policy),
            project=_policy_rules(self._load_project_policy()),
        )
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
        self._mcp_stack = AsyncExitStack()
        self._mcp_tools: list[BaseTool] = []
        self._task_mcp: dict[str, tuple[AsyncExitStack, list[BaseTool]]] = {}
        self._mcp_error: str | None = None
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stream_roles: dict[tuple[str, str], str] = {}
        self._stream_tool_names: dict[tuple[str, str], str] = {}
        self._notification_watchers: set[asyncio.Task[None]] = set()
        self._spawned_task_ids: set[str] = set()
        self._closed = False

    @property
    def model(self) -> str | None:
        try:
            return self._profile().model_id
        except (RuntimeError, KeyError):
            return None

    @property
    def protocol(self) -> str | None:
        try:
            return self._profile().protocol
        except (RuntimeError, KeyError):
            return None

    async def initialize(self) -> "SayacodeApp":
        saved = await self.runtime.get_thread(self.session_id)
        if saved is not None:
            if Path(saved.get("workspace", "")).resolve() != self.workspace:
                raise ValueError("Session belongs to another workspace")
            if not self.mode_explicit:
                self.mode = normalize_mode(str(saved.get("mode") or self.mode))
            elif saved.get("mode") != self.mode:
                saved["mode"] = self.mode
                saved["updated_at"] = _now()
                await self.runtime.store.aput(("threads",), self.session_id, saved, index=False)
        await self.tasks.reconcile_orphans()
        await self._set_active_session(self.session_id)
        await self._ensure_thread(self.session_id, self.mode)
        await self._reload_mcp()
        await self.hooks.trigger("SessionStart", {"thread_id": self.session_id})
        return self

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.tasks.shutdown(timeout=self._shutdown_grace_seconds())
            await self.hooks.trigger("SessionEnd", {"thread_id": self.session_id})
        finally:
            watchers = list(self._notification_watchers)
            for watcher in watchers:
                watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            for stack, _ in self._task_mcp.values():
                await stack.aclose()
            await self._mcp_stack.aclose()
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
        await self._notifications.put(
            {
                "type": f"task.{record.status}",
                "task_id": record.task_id,
                "thread_id": record.thread_id,
                "role": record.role,
                "status": record.status,
                "result": record.result,
                "error": record.error,
            }
        )
        await self.audit.append(
            "task.status",
            thread_id=record.thread_id,
            task_id=record.task_id,
            details=record.to_dict(),
        )

    async def next_notification(self) -> dict[str, Any]:
        """Wait for a task state transition while the CLI is open."""
        return await self._notifications.get()

    def watch_notifications(self, callback: Any) -> asyncio.Task[None]:
        """Deliver task state changes to an open terminal without a background service."""

        async def watch() -> None:
            while True:
                event = await self.next_notification()
                result = callback(event)
                if inspect.isawaitable(result):
                    await result

        task = asyncio.create_task(watch(), name="sayacode-notifications")
        self._notification_watchers.add(task)
        task.add_done_callback(self._notification_watchers.discard)
        return task

    def _load_project_policy(self) -> dict[str, str]:
        path = self.paths.project_policy(self.workspace)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = document.get("tools", document) if isinstance(document, dict) else {}
        return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}

    def _profile(self) -> Profile:
        if self.profile_override is not None:
            return self.profile_override
        if self.profile_name is not None:
            return self.config.profile(self.profile_name)
        return self.config.profile()

    async def _ensure_thread(self, thread_id: str, mode: str) -> None:
        await self._load_thread_policy(thread_id)
        context = self._context(thread_id, mode)
        if await self.runtime.get_thread(thread_id) is None:
            await self.runtime.put_thread(thread_id, context, status="idle", title="New session")

    def _policy_for_thread(self, thread_id: str) -> Policy:
        policy = self._thread_policies.get(thread_id)
        if policy is None:
            policy = Policy(user=self.policy.user, project=self.policy.project)
            self._thread_policies[thread_id] = policy
        return policy

    async def _load_thread_policy(self, thread_id: str) -> Policy:
        if thread_id not in self._thread_policies:
            item = await self.runtime.get_thread(thread_id)
            policy = self._policy_for_thread(thread_id)
            if item is not None:
                policy.session.update(_policy_rules(item.get("session_policy", {})))
        return self._thread_policies[thread_id]

    async def _save_thread_policy(self, thread_id: str) -> None:
        item = await self.runtime.get_thread(thread_id)
        if item is None:
            return
        item["session_policy"] = dict(self._policy_for_thread(thread_id).session)
        item["updated_at"] = _now()
        await self.runtime.store.aput(("threads",), thread_id, item, index=False)

    def _context(
        self,
        thread_id: str,
        mode: str,
        *,
        workspace: Path | None = None,
        task_id: str | None = None,
        background: bool = False,
        profile_name: str | None = None,
    ) -> AgentContext:
        active_workspace = (workspace or self.workspace).resolve()
        return AgentContext(
            workspace=active_workspace,
            mode=normalize_mode(mode),
            policy=self._policy_for_thread(thread_id),
            output_dir=self.paths.outputs,
            session_id=thread_id,
            task_id=task_id,
            profile_name=profile_name or self.profile_name,
            is_background=background,
            output_limit_bytes=self._output_limit_bytes(),
        )

    def _output_limit_bytes(self) -> int:
        try:
            value = int(self.config.preferences.get("output_limit_bytes", "65536"))
        except ValueError:
            return 64 * 1024
        return value if value > 0 else 64 * 1024

    def _shutdown_grace_seconds(self) -> float:
        try:
            value = float(self.config.preferences.get("shutdown_grace_seconds", "10"))
        except ValueError:
            return 10.0
        return value if value > 0 else 10.0

    async def _set_active_session(self, session_id: str) -> None:
        await self.runtime.store.aput(
            ("active_sessions",),
            _workspace_key(self.workspace),
            {"workspace": str(self.workspace), "thread_id": session_id, "updated_at": _now()},
            index=False,
        )

    async def _new_session(self, title: str | None = None) -> str:
        session_id = f"session-{uuid4().hex[:12]}"
        await self._set_active_session(session_id)
        await self._ensure_thread(session_id, self.mode)
        self.session_id = session_id
        if title:
            item = await self.runtime.get_thread(session_id)
            if item is not None:
                item["title"] = title
                item["updated_at"] = _now()
                await self.runtime.store.aput(("threads",), session_id, item, index=False)
        return session_id

    async def _get_handle(
        self,
        *,
        thread_id: str,
        mode: str,
        workspace: Path | None = None,
        task_id: str | None = None,
        background: bool = False,
        include_team_tools: bool = True,
        profile_override: Profile | None = None,
    ) -> tuple[AgentHandle, AgentContext]:
        await self._load_thread_policy(thread_id)
        context = self._context(
            thread_id,
            mode,
            workspace=workspace,
            task_id=task_id,
            background=background,
            profile_name=profile_override.name if profile_override else None,
        )
        profile = profile_override or self._profile()
        explicit_tools = build_tools(context)
        if include_team_tools:
            explicit_tools.extend(self._team_tools())
        mcp_tools = await self._mcp_for_workspace(context.workspace)
        all_tools = [*explicit_tools, *mcp_tools]
        key = (
            hashlib.sha256(repr(asdict(profile)).encode("utf-8")).hexdigest(),
            mode,
            f"{context.workspace}|{task_id or ''}|{','.join(tool.name for tool in all_tools)}",
        )
        handle = self._handles.get(key)
        if handle is None:
            instructions = load_project_instructions(context.workspace, self.paths.memory)
            prefs = PromptPreferences(
                style=self.config.preferences.get("style", "standard"),
                language=self.config.preferences.get("language", "auto"),
                mode=mode,
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
        mode: str | None = None,
        input_format: str = "interactive",
    ) -> dict[str, Any]:
        """Run one non-streaming user turn through the official graph."""
        thread_id = session_id or self.session_id
        active_mode = normalize_mode(mode or self.mode)
        try:
            block = await self.hooks.trigger(
                "UserPromptSubmit", {"prompt": prompt, "thread_id": thread_id}
            )
            if block:
                return {"ok": False, "status": "failed", "error": block, "thread_id": thread_id}
            handle, context = await self._get_handle(thread_id=thread_id, mode=active_mode)
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
            await self.audit.append("run.failed", thread_id=thread_id, details={"error": str(exc)})
            return {"ok": False, "status": "failed", "thread_id": thread_id, "error": str(exc)}

    async def stream(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        mode: str | None = None,
        input_format: str = "interactive",
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one turn and expose a small stable projection of native v3 events."""
        thread_id = session_id or self.session_id
        active_mode = normalize_mode(mode or self.mode)
        try:
            block = await self.hooks.trigger(
                "UserPromptSubmit", {"prompt": prompt, "thread_id": thread_id}
            )
            if block:
                yield {"type": "run.failed", "thread_id": thread_id, "error": block}
                return
            handle, context = await self._get_handle(thread_id=thread_id, mode=active_mode)
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
                    for public in self._normalize_event(event, thread_id):
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
                    "action_requests": self._actions(interrupts),
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
            await self.audit.append("run.failed", thread_id=thread_id, details={"error": str(exc)})
            yield {"type": "run.failed", "thread_id": thread_id, "error": str(exc), "ok": False}

    def _normalize_event(self, event: dict[str, Any], thread_id: str) -> list[dict[str, Any]]:
        """Map v3 envelopes to the intentionally small public event protocol."""
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        data = params.get("data") if isinstance(params, dict) else None
        if method == "messages":
            payload, metadata = (
                (data[0], data[1])
                if isinstance(data, (list, tuple)) and len(data) == 2
                else (data, {})
            )
            if isinstance(payload, AIMessage):
                text = _message_text(payload)
                return (
                    [{"type": "assistant.delta", "thread_id": thread_id, "delta": text}]
                    if text else []
                )
            if not isinstance(payload, dict):
                return []
            run_id = str(metadata.get("run_id") or "") if isinstance(metadata, dict) else ""
            key = (thread_id, run_id)
            kind = payload.get("event")
            if kind == "message-start":
                self._stream_roles[key] = str(payload.get("role") or "")
            elif kind == "message-finish":
                self._stream_roles.pop(key, None)
            elif kind == "content-block-delta" and self._stream_roles.get(key) in {
                "ai", "assistant"
            }:
                delta = payload.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text-delta":
                    delta_text = delta.get("text")
                    if isinstance(delta_text, str) and delta_text:
                        return [{"type": "assistant.delta", "thread_id": thread_id,
                                 "delta": delta_text}]
        if method == "tools":
            payload = data if isinstance(data, dict) else {}
            kind = payload.get("event")
            call_id = str(payload.get("tool_call_id") or "")
            key = (thread_id, call_id)
            if kind == "tool-started":
                name = str(payload.get("tool_name") or "tool")
                self._stream_tool_names[key] = name
                return [
                    {"type": "tool.started", "thread_id": thread_id,
                     "tool_name": name, "tool_call_id": call_id}
                ]
            if kind not in {"tool-error", "tool-finished"}:
                return []
            name = self._stream_tool_names.pop(key, str(payload.get("tool_name") or "tool"))
            if kind == "tool-error":
                return [
                    {
                        "type": "tool.failed",
                        "thread_id": thread_id,
                        "tool_name": name,
                        "tool_call_id": call_id,
                        "error": str(payload.get("message") or "Tool failed"),
                    }
                ]
            if kind == "tool-finished":
                output = payload.get("output")
                if getattr(output, "status", None) == "error":
                    return [{"type": "tool.failed", "thread_id": thread_id,
                             "tool_name": name, "tool_call_id": call_id,
                             "error": _message_text(output)}]
                return [{"type": "tool.completed", "thread_id": thread_id,
                         "tool_name": name, "tool_call_id": call_id}]
        return []

    @staticmethod
    def _actions(interrupts: list[Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for interrupt in interrupts or []:
            value = getattr(interrupt, "value", interrupt)
            if isinstance(value, dict):
                candidate = value.get("action_requests", [])
                if isinstance(candidate, list):
                    actions.extend(item for item in candidate if isinstance(item, dict))
        return actions

    def _team_tools(self) -> list[BaseTool]:
        @tool
        async def delegate_to_subagent(
            task: str,
            runtime: ToolRuntime[Any],
            role: Literal["builder", "planner", "reviewer"] = "planner",
        ) -> dict[str, Any]:
            """Start an independent background coding, planning, or review task."""
            context = runtime.context
            record = await self._spawn_task(
                task,
                role=role,
                parent_thread_id=context.session_id,
                profile_name=context.profile_name,
            )
            return {
                "task_id": record.task_id,
                "thread_id": record.thread_id,
                "status": record.status,
                "workspace": record.task_workspace or record.workspace,
            }

        @tool
        async def task_status(task_id: str) -> dict[str, Any]:
            """Get an independent task's checkpointed status and result."""
            return (await self.tasks.get(task_id)).to_dict()

        @tool
        async def task_delivery(task_id: str) -> dict[str, Any]:
            """Inspect a write task's explicit delivery diff without applying it."""
            return await self.tasks.delivery(task_id)

        @tool
        async def task_wait(task_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
            """Wait briefly for a background task and return its latest status and result."""
            if not 0 <= timeout_seconds <= 300:
                raise ValueError("timeout_seconds must be between 0 and 300")
            records = await self.tasks.wait_active([task_id], timeout=timeout_seconds)
            return records[0].to_dict()

        return [delegate_to_subagent, task_status, task_delivery, task_wait]

    async def _spawn_task(
        self,
        prompt: str,
        *,
        role: str,
        parent_thread_id: str | None,
        profile_name: str | None = None,
    ) -> TaskRecord:
        if role not in {"builder", "planner", "reviewer"}:
            raise TaskError("role must be builder, planner, or reviewer")
        parent = await self.runtime.get_thread(parent_thread_id) if parent_thread_id else None
        parent_mode = normalize_mode(str((parent or {}).get("mode") or self.mode))
        task_mode = parent_mode if role == "builder" else {
            "planner": "plan", "reviewer": "review"
        }[role]
        record = await self.tasks.spawn(
            parent_thread_id=parent_thread_id,
            role=role,
            prompt=prompt,
            workspace=self.workspace,
            write_access=role == "builder" and task_mode == "build",
            runner=self._task_runner,
            profile_name=profile_name or self.profile_name,
            mode=task_mode,
            profile_snapshot=asdict(self._profile()),
        )
        self._spawned_task_ids.add(record.task_id)
        return record

    async def _task_runner(self, record: TaskRecord, control: RunControl) -> str | None:
        mode = record.mode or {"builder": "build", "planner": "plan", "reviewer": "review"}[record.role]
        workspace = Path(record.task_workspace or record.workspace)
        profile = (
            Profile.from_dict(record.profile_snapshot)
            if record.profile_snapshot is not None
            else self.config.profile(record.profile_name)
            if record.profile_name in self.config.profiles
            else self.profile_override
        )
        handle, context = await self._get_handle(
            thread_id=record.thread_id,
            mode=mode,
            workspace=workspace,
            task_id=record.task_id,
            background=True,
            include_team_tools=False,
            profile_override=profile,
        )
        try:
            message = record.pending_input
            record.pending_input = None
            await self.tasks.update(record)
            role_instruction = {
                "builder": "You are the builder. Implement and verify the requested change in your assigned worktree. Do not apply delivery to the parent workspace.",
                "planner": "You are the planner. Inspect the assigned workspace read-only and return a concrete implementation plan.",
                "reviewer": "You are the reviewer. Inspect the assigned workspace read-only and report actionable findings with file evidence.",
            }[record.role]
            if record.role == "builder" and not record.write_access:
                role_instruction = (
                    "You are a read-only builder. Inspect the workspace and describe the "
                    "implementation needed. Do not edit files or run mutating commands."
                )
            if message:
                message = f"{role_instruction}\n\nTask:\n{message}"
            result = await self.runtime.invoke(
                handle,
                context,
                message,
                thread_id=record.thread_id,
                control=control,
                callbacks=[self._audit_callback(record.thread_id, record.task_id)],
            )
        except GraphDrained:
            raise
        if result.interrupts:
            raise TaskPaused("Task requires approval")
        return _final_text(result)

    async def wait_for_tasks(self) -> list[dict[str, Any]]:
        """Wait for this CLI process's child graph runs to settle or pause."""
        selected = sorted(self._spawned_task_ids)
        records = await self.tasks.wait_active(selected)
        self._spawned_task_ids.difference_update(selected)
        return [record.to_dict() for record in records]

    async def command(self, name: str, args: Any = "") -> Any:
        """Command surface used by the terminal; all operations stay thin adapters."""
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
            handle, context = await self._get_handle(thread_id=self.session_id, mode=self.mode)
            return {
                "compacted": await self.runtime.compact(
                    handle, context, thread_id=self.session_id,
                    focus=str(args).strip() or None,
                )
            }
        if command == "rewind":
            return await self._rewind(args)
        if command == "reset":
            return {"session_id": await self._new_session()}
        if command == "mode":
            self.mode = normalize_mode(str(args or "build"))
            item = await self.runtime.get_thread(self.session_id)
            if item is not None:
                item["mode"] = self.mode
                item["updated_at"] = _now()
                await self.runtime.store.aput(("threads",), self.session_id, item, index=False)
            self._handles.clear()
            return {"mode": self.mode}
        if command == "model" and str(args or "").strip() in self.config.profiles:
            return await self._config_command(f"use {str(args).strip()}")
        if command in {"config", "model"}:
            return await self._config_command(args)
        if command == "permissions":
            return await self._policy_command(args)
        if command == "mcp":
            return await self._mcp_command(args)
        if command == "tools":
            catalog = tool_catalog([*build_tools(), *self._mcp_tools, *self._team_tools()])
            requested = str(args or "").strip()
            if requested:
                return next(
                    (item for item in catalog if item["name"] == requested),
                    {"ok": False, "error": f"Unknown tool: {requested}"},
                )
            return catalog
        if command == "plan":
            return await self._plan()
        if command == "team":
            return await self._team_command(args)
        if command == "trace":
            rows = await self.audit.list(thread_id=self.session_id)
            requested = str(args or "").strip()
            return (
                [row for row in rows if row.get("run_id") == requested or
                 row.get("details", {}).get("parent_run_id") == requested]
                if requested else rows
            )
        if command == "doctor":
            return await self._doctor(args)
        if command == "git":
            return await self._git_command(args)
        if command == "analyze":
            return await self._invoke_native_tool("analyze_project")
        if command == "symbols":
            query = str(args or "").strip()
            return await self._invoke_native_tool(
                "find_symbol" if query else "list_symbols", name=query
            )
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
        tokens = shlex.split(str(args or ""))
        action = tokens[0].lower() if tokens else "status"
        scope = tokens[1].lower() if len(tokens) > 1 else "user"
        target = self.paths.memory if scope == "user" else self.workspace / "SAYACODE.md"
        if scope not in {"user", "project"}:
            raise ValueError("memory scope must be user or project")
        if action == "status":
            instructions = load_project_instructions(self.workspace, self.paths.memory)
            return {
                "user_memory": str(self.paths.memory),
                "project_memory": str(self.workspace / "SAYACODE.md"),
                "loaded_characters": len(instructions),
            }
        if action == "init":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
            return {"initialized": str(target)}
        if action == "append":
            if len(tokens) < 3:
                raise ValueError("Usage: /memory append <user|project> <text>")
            append_user_memory(target, " ".join(tokens[2:]))
            self._handles.clear()
            return {"appended": str(target)}
        raise ValueError("Usage: /memory [status|init <user|project>|append <user|project> <text>]")

    async def _settings_command(self, args: Any) -> dict[str, Any]:
        tokens = shlex.split(str(args or ""))
        if not tokens or tokens[0] == "show":
            return {
                "preferences": dict(self.config.preferences),
                "profile": self.profile_name,
                "mode": self.mode,
                "output_limit_bytes": self._output_limit_bytes(),
                "shutdown_grace_seconds": self._shutdown_grace_seconds(),
            }
        if len(tokens) != 3 or tokens[0] != "set":
            raise ValueError(
                "Usage: /settings [show|set output_limit_bytes <bytes>|"
                "set shutdown_grace_seconds <seconds>]"
            )
        key, raw = tokens[1:]
        value: int | float
        if key == "output_limit_bytes":
            value = int(raw)
        elif key == "shutdown_grace_seconds":
            value = float(raw)
        else:
            raise ValueError(f"Unknown setting: {key}")
        if value <= 0:
            raise ValueError(f"{key} must be positive")
        self.config.preferences[key] = str(value)
        await self._save_config()
        self._handles.clear()
        return {key: value}

    async def _context_for_thread(self, thread_id: str) -> tuple[AgentHandle, AgentContext]:
        metadata = await self.runtime.get_thread(thread_id)
        if metadata is None:
            raise KeyError(f"Unknown thread: {thread_id}")
        return await self._get_handle(
            thread_id=thread_id,
            mode=str(metadata.get("mode") or self.mode),
            workspace=Path(metadata.get("workspace") or self.workspace),
            task_id=metadata.get("task_id"),
            background=bool(metadata.get("is_background")),
            include_team_tools=not bool(metadata.get("is_background")),
        )

    async def _resume_approval(self, command: str, args: Any) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ValueError("Approval payload must be an object")
        thread_id = str(args.get("thread_id") or self.session_id)
        decisions = args.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise ValueError("Approval requires at least one decision")
        if command == "reject":
            decisions = [
                item
                if isinstance(item, dict) and item.get("type") == "reject"
                else {"type": "reject", "message": "Declined by user"}
                for item in decisions
            ]
        handle, context = await self._context_for_thread(thread_id)
        snapshot = await self.runtime.get_state(handle, thread_id)
        actions = self._actions(list(snapshot.interrupts))
        if len(actions) != len(decisions):
            raise ValueError("Approval decisions do not match pending actions")
        grants = args.get("grants", [])
        if not isinstance(grants, list):
            raise ValueError("Approval grants must be a list")
        validated_grants: list[tuple[str, dict[str, Any], str]] = []
        for grant in grants:
            if not isinstance(grant, dict):
                raise ValueError("Invalid approval grant")
            index = grant.get("index")
            scope = grant.get("scope")
            if not isinstance(index, int) or not 0 <= index < len(actions):
                raise ValueError("Approval grant index is out of range")
            if scope not in {"session", "user", "project"}:
                raise ValueError("Invalid approval grant scope")
            if decisions[index].get("type") != "approve":
                raise ValueError("Cannot grant a rejected action")
            action = actions[index]
            name = str(action.get("name") or "")
            if not name or name != grant.get("tool_name"):
                raise ValueError("Approval grant tool does not match pending action")
            arguments = action.get("args", {})
            if not isinstance(arguments, dict):
                raise ValueError("Approval action arguments must be an object")
            validated_grants.append((name, arguments, scope))
        result = await self.runtime.invoke(
            handle,
            context,
            thread_id=thread_id,
            resume={"decisions": decisions},
            callbacks=[self._audit_callback(thread_id, context.task_id)],
        )
        policy = await self._load_thread_policy(thread_id)
        changed_scopes: set[str] = set()
        for name, arguments, scope in validated_grants:
            policy.grant_call(name, arguments, context, scope=scope)
            changed_scopes.add(scope)
        if "session" in changed_scopes:
            await self._save_thread_policy(thread_id)
        if "user" in changed_scopes:
            self.config.user_policy = dict(self.policy.user)
            await self._save_config()
        if "project" in changed_scopes:
            path = self.paths.project_policy(self.workspace)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"tools": self.policy.project}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if result.interrupts:
            return {
                "ok": False,
                "status": "paused",
                "thread_id": thread_id,
                "interrupts": result.interrupts,
            }
        response = _final_text(result)
        await self.audit.append(
            "run.resumed", thread_id=thread_id, details={"response_chars": len(response)}
        )
        task = await self._task_by_thread(thread_id)
        if task is not None and task.status == "paused":
            task.status = "completed"
            task.result = response
            await self.tasks.update(task)
        return {"ok": True, "status": "completed", "thread_id": thread_id, "response": response}

    async def _task_by_thread(self, thread_id: str) -> TaskRecord | None:
        for record in await self.tasks.list(workspace=self.workspace):
            if record.thread_id == thread_id:
                return record
        return None

    async def _status(self) -> dict[str, Any]:
        threads = await self.runtime.list_threads(workspace=self.workspace)
        current = await self.runtime.get_thread(self.session_id)
        active_tasks = [
            record.to_dict()
            for record in await self.tasks.list(workspace=self.workspace)
            if record.status in {"pending", "running", "stopping", "paused"}
        ]
        messages: list[Any] = []
        try:
            handle, _ = await self._context_for_thread(self.session_id)
            state = await self.runtime.get_state(handle, self.session_id)
            messages = list(state.values.get("messages", [])) if state.values else []
        except (KeyError, RuntimeError):
            pass
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        has_usage = False
        for message in messages:
            raw = getattr(message, "usage_metadata", None)
            if not isinstance(raw, dict):
                continue
            has_usage = True
            for key in usage:
                usage[key] += int(raw.get(key, 0) or 0)
        return {
            "ok": True,
            "workspace": str(self.workspace),
            "session_id": self.session_id,
            "mode": self.mode,
            "profile": self.profile_name,
            "model": self.model,
            "protocol": self.protocol,
            "base_url": self._profile().base_url if self.model is not None else None,
            "context_length": self._profile().context_length if self.model is not None else None,
            "max_output_tokens": self._profile().max_output_tokens if self.model is not None else None,
            "thread": current,
            "sessions": len([item for item in threads if not item.get("is_background")]),
            "active_tasks": active_tasks,
            "mcp_tools": [item.name for item in self._mcp_tools],
            "mcp_error": self._mcp_error,
            "message_count": len(messages),
            "usage": usage if has_usage else None,
        }

    async def _session_command(self, args: Any) -> Any:
        tokens = shlex.split(str(args or ""))
        action = tokens[0].lower() if tokens else "current"
        if action in {"current", "show"}:
            return await self.runtime.get_thread(self.session_id)
        if action in {"list", "sessions"}:
            return [
                item
                for item in await self.runtime.list_threads(workspace=self.workspace)
                if not item.get("is_background")
            ]
        if action in {"new", "create"}:
            return {"session_id": await self._new_session(" ".join(tokens[1:]) or None)}
        if action in {"use", "switch"}:
            if len(tokens) != 2:
                raise ValueError("Usage: /session use <thread-id>")
            thread_id = tokens[1]
            item = await self.runtime.get_thread(thread_id)
            if item is None or Path(item.get("workspace", "")).resolve() != self.workspace:
                raise KeyError(f"Unknown session: {thread_id}")
            self.session_id = thread_id
            self.mode = str(item.get("mode") or self.mode)
            await self._set_active_session(thread_id)
            return item
        if action == "rename":
            if len(tokens) < 2:
                raise ValueError("Usage: /session rename <title>")
            item = await self.runtime.get_thread(self.session_id)
            if item is None:
                raise KeyError(self.session_id)
            item["title"] = " ".join(tokens[1:])
            item["updated_at"] = _now()
            await self.runtime.store.aput(("threads",), self.session_id, item, index=False)
            return item
        raise ValueError("Usage: /session [current|list|new|use|rename]")

    async def _history(self) -> list[dict[str, Any]]:
        handle, _ = await self._context_for_thread(self.session_id)
        state = await self.runtime.get_state(handle, self.session_id)
        messages = state.values.get("messages", []) if state.values else []
        return [
            {
                "id": getattr(message, "id", None),
                "role": getattr(message, "type", type(message).__name__),
                "content": _message_text(message),
                "status": getattr(message, "status", None),
            }
            for message in messages
        ]

    async def _rewind(self, args: Any) -> Any:
        handle, _ = await self._context_for_thread(self.session_id)
        history = await self.runtime.get_history(handle, self.session_id)
        token = str(args or "").strip()
        if not token:
            return [
                {
                    "index": index,
                    "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id"),
                    "next": list(snapshot.next),
                }
                for index, snapshot in enumerate(history)
            ]
        selected = None
        if token.isdigit():
            index = int(token)
            if 0 <= index < len(history):
                selected = history[index]
        if selected is None:
            selected = next(
                (
                    snapshot
                    for snapshot in history
                    if snapshot.config.get("configurable", {}).get("checkpoint_id") == token
                ),
                None,
            )
        if selected is None:
            raise KeyError(f"Unknown checkpoint: {token}")
        checkpoint_id = selected.config["configurable"]["checkpoint_id"]
        fork = await self.runtime.rewind(handle, self.session_id, checkpoint_id)
        return {"rewound": True, "checkpoint": checkpoint_id, "fork": fork}

    async def _config_command(self, args: Any) -> Any:
        if isinstance(args, dict):
            if args.get("action") != "add" or not isinstance(args.get("profile"), dict):
                raise ValueError("Model command object must contain action=add and a profile")
            raw = args["profile"]
            expected = {
                "protocol", "base_url", "api_key", "model_id",
                "context_length", "max_output_tokens",
            }
            if set(raw) != expected:
                raise ValueError(
                    f"Model profile requires exactly: {', '.join(sorted(expected))}"
                )
            model_id = raw["model_id"]
            if not isinstance(model_id, str):
                raise ValueError("model_id must be a string")
            name = _new_profile_name(model_id, self.config.profiles)
            profile = Profile(name=name, **raw)
            self.config.profiles[name] = profile
            self.config.default_profile = self.config.default_profile or name
            self.profile_name = self.config.default_profile
            self.profile_override = None
            self._handles.clear()
            await self._save_config()
            return {"added": name, "default_profile": self.config.default_profile}
        tokens = shlex.split(str(args or ""))
        action = tokens[0].lower() if tokens else "list"
        if action in {"list", "profiles"}:
            return {
                "default_profile": self.config.default_profile,
                "profiles": {
                    name: asdict(profile) | {"api_key": "***" if profile.api_key else None}
                    for name, profile in self.config.profiles.items()
                },
            }
        if action in {"show", "current"}:
            profile = self._profile()
            item = asdict(profile)
            if item.get("api_key"):
                item["api_key"] = "***"
            return item
        if action in {"use", "switch"}:
            if len(tokens) != 2:
                raise ValueError("Usage: /config use <profile>")
            self.config.profile(tokens[1])
            self.config.default_profile = tokens[1]
            self.profile_name = tokens[1]
            self.profile_override = None
            self._handles.clear()
            await self._save_config()
            return {"default_profile": tokens[1]}
        if action in {"remove", "delete"}:
            if len(tokens) != 2:
                raise ValueError("Usage: /config remove <profile>")
            name = tokens[1]
            if name not in self.config.profiles:
                raise KeyError(name)
            del self.config.profiles[name]
            if self.config.default_profile == name:
                self.config.default_profile = next(iter(self.config.profiles), None)
            self.profile_name = self.config.default_profile
            self._handles.clear()
            await self._save_config()
            return {"removed": name, "default_profile": self.config.default_profile}
        if action == "add":
            raise ValueError("Use interactive /model add to enter the six protocol fields")
        if action == "test":
            profile = self.config.profile(tokens[1]) if len(tokens) > 1 else self._profile()
            model = self.runtime._model_for(
                profile, self.model_override if len(tokens) == 1 else None
            )
            report: dict[str, Any] = {
                "profile": profile.name,
                "protocol": profile.protocol,
                "text": False,
                "tool_calling": False,
                "stream": False,
                "errors": {},
            }
            try:
                response = await asyncio.wait_for(
                    model.ainvoke("Reply with exactly: OK"), timeout=60
                )
                report["text"] = bool(_message_text(response).strip())
                report["response"] = _message_text(response)[:200]
            except Exception as exc:
                report["errors"]["text"] = f"{type(exc).__name__}: {exc}"
                report["ok"] = False
                return report

            @tool
            def sayacode_capability_probe(value: str) -> str:
                """Return the supplied value to test model tool calling."""
                return value

            try:
                bound = model.bind_tools([sayacode_capability_probe])
                tool_response = await asyncio.wait_for(
                    bound.ainvoke(
                        "Call sayacode_capability_probe with value 'ping'."
                    ),
                    timeout=60,
                )
                calls = getattr(tool_response, "tool_calls", [])
                report["tool_calling"] = any(
                    call.get("name") == "sayacode_capability_probe" for call in calls
                )
            except Exception as exc:
                report["errors"]["tool_calling"] = f"{type(exc).__name__}: {exc}"
            try:
                chunks = []
                async for chunk in model.astream("Reply with exactly: OK"):
                    chunks.append(_message_text(chunk))
                report["stream"] = bool("".join(chunks).strip())
            except Exception as exc:
                report["errors"]["stream"] = f"{type(exc).__name__}: {exc}"
            report["ok"] = all(report[key] for key in ("text", "tool_calling", "stream"))
            return report
        raise ValueError("Usage: /config [list|show|add|use|remove|test]")

    async def _save_config(self) -> None:
        await self.repository.save(self.config)

    async def _policy_command(self, args: Any) -> Any:
        tokens = shlex.split(str(args or ""))
        action = tokens[0].lower() if tokens else "show"
        policy = await self._load_thread_policy(self.session_id)
        if action in {"show", "status"}:
            return {
                "user": dict(policy.user),
                "project": dict(policy.project),
                "session": dict(policy.session),
                "default": "workspace reads/edits allow; deletion, shell, Git changes and external tools ask",
            }
        if action == "clear":
            if len(tokens) != 2 or tokens[1] != "session":
                raise ValueError("Usage: /permissions clear session")
            policy.session.clear()
            await self._save_thread_policy(self.session_id)
            self._handles.clear()
            return {"cleared": "session"}
        if action in {"set", "allow", "ask", "deny"}:
            if action == "set":
                if len(tokens) < 4:
                    raise ValueError(
                        "Usage: /permissions set <scope> <tool> <action> [path=<glob>] [command=<glob>]"
                    )
                scope, name, decision = tokens[1:4]
                selectors = tokens[4:]
            else:
                if len(tokens) < 3:
                    raise ValueError(
                        f"Usage: /permissions {action} <scope> <tool> "
                        "[path=<glob>] [command=<glob>]"
                    )
                decision, scope, name = action, tokens[1], tokens[2]
                selectors = tokens[3:]
            options: dict[str, str] = {}
            for selector in selectors:
                key, separator, value = selector.partition("=")
                if not separator or key not in {"path", "command"} or not value:
                    raise ValueError("Permission selector must be path=<glob> or command=<glob>")
                options[key] = value
            policy.set_rule(name, decision, scope, **options)
            if scope == "user":
                self.config.user_policy = dict(policy.user)
                await self._save_config()
            elif scope == "project":
                path = self.paths.project_policy(self.workspace)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"tools": policy.project}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            elif scope == "session":
                await self._save_thread_policy(self.session_id)
            self._handles.clear()
            return {"scope": scope, "tool": name, "action": decision, **options}
        if action == "audit":
            return await self.audit.list(thread_id=self.session_id)
        raise ValueError("Usage: /permissions [show|set|allow|ask|deny|clear session|audit]")

    def _project_mcp_servers(self, workspace: Path | None = None) -> dict[str, dict[str, Any]]:
        path = (workspace or self.workspace) / ".mcp.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        servers = raw.get("mcpServers", raw) if isinstance(raw, dict) else {}
        return {
            str(name): dict(value) for name, value in servers.items() if isinstance(value, dict)
        }

    @property
    def _mcp_trusted(self) -> bool:
        return str(self.workspace) in set(self.config.trusted_mcp_projects)

    def _mcp_config_for(self, workspace: Path) -> dict[str, dict[str, Any]]:
        project = self._project_mcp_servers(workspace) if self._mcp_trusted else {}
        servers = {**self.config.mcp_servers, **project}
        configured: dict[str, dict[str, Any]] = {}
        for name, server in servers.items():
            value = dict(server)
            if "command" in value:
                value["cwd"] = str(workspace)
            configured[name] = value
        return configured

    async def _mcp_for_workspace(self, workspace: Path) -> list[BaseTool]:
        if workspace == self.workspace:
            return self._mcp_tools
        key = str(workspace)
        cached = self._task_mcp.get(key)
        if cached is not None:
            return cached[1]
        servers = self._mcp_config_for(workspace)
        if not servers:
            return []
        from langchain.mcp import MCPAdapter

        stack = AsyncExitStack()
        try:
            adapter = MCPAdapter({"mcpServers": servers})
            await stack.enter_async_context(adapter)
            tools = namespace_mcp_tools(await adapter.list_tools())
        except BaseException:
            await stack.aclose()
            raise
        self._task_mcp[key] = (stack, tools)
        return tools

    async def _reload_mcp(self) -> list[BaseTool]:
        for stack, _ in self._task_mcp.values():
            await stack.aclose()
        self._task_mcp.clear()
        await self._mcp_stack.aclose()
        self._mcp_stack = AsyncExitStack()
        self._mcp_tools = []
        self._mcp_error = None
        project_servers = self._project_mcp_servers()
        if project_servers and not self._mcp_trusted:
            self._mcp_error = "Project MCP configuration is untrusted; run /mcp trust first"
        servers = self._mcp_config_for(self.workspace)
        if not servers:
            self._handles.clear()
            return []
        try:
            from langchain.mcp import MCPAdapter

            adapter = MCPAdapter({"mcpServers": servers})
            await self._mcp_stack.enter_async_context(adapter)
            self._mcp_tools = namespace_mcp_tools(await adapter.list_tools())
        except Exception as exc:
            self._mcp_error = str(exc)
            self._mcp_tools = []
        self._handles.clear()
        return list(self._mcp_tools)

    async def _mcp_command(self, args: Any) -> Any:
        tokens = shlex.split(str(args or ""))
        action = tokens[0].lower() if tokens else "status"
        if action == "status":
            return {
                "trusted": self._mcp_trusted,
                "project_servers": sorted(self._project_mcp_servers()),
                "user_servers": sorted(self.config.mcp_servers),
                "tools": [
                    {"name": tool.name, "description": tool.description} for tool in self._mcp_tools
                ],
                "error": self._mcp_error,
            }
        if action == "trust":
            trusted = set(self.config.trusted_mcp_projects)
            trusted.add(str(self.workspace))
            self.config.trusted_mcp_projects = sorted(trusted)
            await self._save_config()
            await self._reload_mcp()
            return {"trusted": True, "tools": [tool.name for tool in self._mcp_tools]}
        if action == "untrust":
            trusted = set(self.config.trusted_mcp_projects)
            trusted.discard(str(self.workspace))
            self.config.trusted_mcp_projects = sorted(trusted)
            await self._save_config()
            await self._reload_mcp()
            return {"trusted": False}
        if action == "reload":
            await self._reload_mcp()
            return {"tools": [tool.name for tool in self._mcp_tools], "error": self._mcp_error}
        if action == "add":
            if len(tokens) < 3:
                raise ValueError("Usage: /mcp add <name> <command> [arguments...]")
            name, executable = tokens[1:3]
            self.config.mcp_servers[name] = {"command": executable, "args": tokens[3:]}
            await self._save_config()
            await self._reload_mcp()
            return {"added": name, "error": self._mcp_error}
        if action in {"remove", "delete"}:
            if len(tokens) != 2:
                raise ValueError("Usage: /mcp remove <name>")
            self.config.mcp_servers.pop(tokens[1], None)
            await self._save_config()
            await self._reload_mcp()
            return {"removed": tokens[1]}
        raise ValueError("Usage: /mcp [status|trust|untrust|reload|add|remove]")

    async def _plan(self) -> Any:
        handle, _ = await self._context_for_thread(self.session_id)
        state = await self.runtime.get_state(handle, self.session_id)
        return list(state.values.get("todos", [])) if state.values else []

    async def _team_command(self, args: Any) -> Any:
        tokens = shlex.split(str(args or ""))
        action = tokens[0].lower() if tokens else "list"
        if action in {"list", "status"}:
            if action == "status" and len(tokens) == 2:
                return (await self.tasks.get(tokens[1])).to_dict()
            return [record.to_dict() for record in await self.tasks.list(workspace=self.workspace)]
        if action == "spawn":
            if len(tokens) < 3:
                raise ValueError("Usage: /team spawn <builder|planner|reviewer> <task>")
            role = tokens[1].lower()
            prompt = " ".join(tokens[2:])
            return (
                await self._spawn_task(prompt, role=role, parent_thread_id=self.session_id)
            ).to_dict()
        if action == "wait":
            if len(tokens) != 2:
                raise ValueError("Usage: /team wait <task-id>")
            return (await self.tasks.wait(tokens[1])).to_dict()
        if action == "pending":
            if len(tokens) != 2:
                raise ValueError("Usage: /team pending <task-id>")
            record = await self.tasks.get(tokens[1])
            if record.status != "paused":
                return {"task_id": record.task_id, "status": record.status, "action_requests": []}
            handle, _ = await self._context_for_thread(record.thread_id)
            state = await self.runtime.get_state(handle, record.thread_id)
            return {
                "task_id": record.task_id,
                "thread_id": record.thread_id,
                "status": record.status,
                "action_requests": self._actions(list(state.interrupts)),
            }
        if action == "stop":
            if len(tokens) != 2:
                raise ValueError("Usage: /team stop <task-id>")
            return (await self.tasks.stop(tokens[1])).to_dict()
        if action == "resume":
            if len(tokens) != 2:
                raise ValueError("Usage: /team resume <task-id>")
            record = await self.tasks.get(tokens[1])
            if record.status == "paused":
                raise TaskError(
                    "Task is waiting for approval; approve or reject its pending action"
                )
            if record.status == "failed":
                raise TaskError("Failed tasks require /team followup with a new instruction")
            return (await self.tasks.resume(record.task_id, self._task_runner)).to_dict()
        if action in {"followup", "follow-up"}:
            if len(tokens) < 3:
                raise ValueError("Usage: /team followup <task-id> <message>")
            record = await self.tasks.get(tokens[1])
            if record.status == "paused":
                raise TaskError(
                    "Task is waiting for approval; approve or reject before a follow-up"
                )
            return (
                await self.tasks.resume(
                    record.task_id, self._task_runner, prompt=" ".join(tokens[2:])
                )
            ).to_dict()
        if action in {"diff", "delivery"}:
            if len(tokens) != 2:
                raise ValueError("Usage: /team diff <task-id>")
            return await self.tasks.delivery(tokens[1])
        if action == "apply":
            if len(tokens) != 2:
                raise ValueError("Usage: /team apply <task-id>")
            return await self.tasks.apply_delivery(tokens[1])
        if action == "cleanup":
            if len(tokens) != 2:
                raise ValueError("Usage: /team cleanup <task-id>")
            return (await self.tasks.remove_worktree(tokens[1])).to_dict()
        raise ValueError("Usage: /team [list|spawn|wait|stop|resume|followup|diff|apply|cleanup]")

    async def _doctor(self, bundle: Any = "") -> dict[str, Any]:
        import shutil
        import sys

        checks = {
            "python": sys.version.split()[0],
            "workspace_exists": self.workspace.is_dir(),
            "git": shutil.which("git") is not None,
            "powershell": shutil.which("pwsh") is not None
            or shutil.which("powershell") is not None,
            "profile_configured": self.profile_name is not None
            or self.config.default_profile is not None,
            "mcp": self._mcp_error is None,
            "checkpoints": self.paths.checkpoints.exists(),
            "store": self.paths.store.exists(),
        }
        result = {"ok": all(checks.values()), "checks": checks, "mcp_error": self._mcp_error}
        target = Path(str(bundle)).expanduser() if str(bundle).strip() else None
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        **result,
                        "workspace": str(self.workspace),
                        "profile": self.profile_name,
                        "recent_audit": await self.audit.list(thread_id=self.session_id, limit=20),
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            result["bundle"] = str(target.resolve())
        return result

    async def _git_command(self, args: Any) -> Any:
        tokens = shlex.split(str(args or ""))
        action = tokens[0] if tokens else "status"
        if action not in {"status", "diff", "log", "branch", "remote", "show"}:
            return {
                "ok": False, "action": "ask",
                "reason": "Git changes require approval through the agent tool call",
            }
        if action in {"status", "branch", "remote"} and len(tokens) != 1:
            raise ValueError(f"/git {action} accepts no arguments")
        options: dict[str, Any] = {"action": action}
        if action in {"diff", "show"} and len(tokens) > 1:
            options["ref"] = tokens[1]
            if len(tokens) > 2:
                options["paths"] = tokens[2:]
        if action == "log" and len(tokens) > 1:
            options["limit"] = int(tokens[1])
        return await self._invoke_native_tool("git", **options)

    async def _invoke_native_tool(self, tool_name: str, **arguments: Any) -> Any:
        """Run a read-only slash-command helper through the same policy and context."""
        context = self._context(self.session_id, self.mode)
        decision = context.policy.decide(tool_name, arguments, context)
        if decision.action != "allow":
            return {"ok": False, "action": decision.action, "reason": decision.reason}
        tools = {item.name: item for item in build_tools(context)}
        selected = tools[tool_name]
        runtime: ToolRuntime[Any] = ToolRuntime(
            state={},
            context=context,
            config={},
            stream_writer=lambda _data: None,
            tool_call_id=None,
            store=self.runtime.store,
            tools=list(tools.values()),
        )
        fn = getattr(selected, "coroutine", None) or getattr(selected, "func", None)
        if not callable(fn):
            raise RuntimeError(f"Tool has no callable implementation: {tool_name}")
        result = fn(runtime=runtime, **arguments)
        return await result if inspect.isawaitable(result) else result


async def create_app(args: Any) -> SayacodeApp:
    """Create the default local application from parsed CLI arguments."""
    paths = AppPaths.resolve()
    repository = ConfigRepository(paths.home)
    config = await repository.load()
    workspace = Path(args.workspace).expanduser().resolve()
    profile_override: Profile | None = None
    profile_name = getattr(args, "profile", None) or config.default_profile
    required = ("protocol", "base_url", "model_id", "context_length", "max_output_tokens")
    supplied = [
        key for key in (*required, "api_key")
        if getattr(args, key, None) is not None
    ]
    if supplied:
        if getattr(args, "profile", None):
            raise ValueError("--profile cannot be combined with one-run model endpoint fields")
        missing = [key for key in required if getattr(args, key, None) is None]
        if missing:
            raise ValueError("Incomplete model endpoint: missing " + ", ".join(missing))
        profile_override = Profile(
            name="command-line",
            protocol=str(args.protocol),
            base_url=str(args.base_url),
            api_key=getattr(args, "api_key", None) or None,
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
        mode=getattr(args, "mode", None) or config.preferences.get("mode", "build"),
        mode_explicit=bool(getattr(args, "mode", None)),
        profile_name=profile_name,
        profile_override=profile_override,
    )
    try:
        return await app.initialize()
    except BaseException:
        await runtime.close()
        raise


__all__ = ["SayacodeApp", "create_app"]
