"""会话目录、信任档位和原生检查点操作。策略内存一份存盘一份，内存没有就从存盘恢复，改完要显式存回。"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence
from uuid import uuid4

from langgraph.runtime import RunControl

from .agent import AgentContext, AgentHandle
from .agent.events import _final_text, _message_text, action_requests
from .approvals import Policy, normalize_trust
from .prompts import AgentRole, normalize_agent_role

if TYPE_CHECKING:
    from .application import SayacodeApp


def _now() -> str:
    # 取世界时 ISO 时间给存盘字段用，保证多机器对得上。
    return datetime.now(UTC).isoformat()


def _workspace_key(workspace: Path) -> str:
    # 给工作区算短指纹，用来找该工作区上次停在哪会话。
    return hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:24]


async def _ensure_thread(app: SayacodeApp, thread_id: str, trust_level: str) -> None:
    # 保证会话在内存策略和运行时里都存在，缺哪个补哪个，新会话起个空标题。
    await app._load_thread_policy(thread_id, trust_level=trust_level)
    context = app._context(thread_id, trust_level)
    if await app.runtime.get_thread(thread_id) is None:
        await app.runtime.put_thread(thread_id, context, status="idle", title="New session")


def _policy_for_thread(app: SayacodeApp, thread_id: str, trust_level: str | None = None) -> Policy:
    # 拿会话内存策略，没有就按传入或应用默认值新建，记住的批准只活在内存里。
    policy = app._thread_policies.get(thread_id)
    if policy is None:
        policy = Policy(trust_level=normalize_trust(trust_level or app.trust_level))
        app._thread_policies[thread_id] = policy
    return policy


async def _load_thread_policy(
    app: SayacodeApp, thread_id: str, *, trust_level: str | None = None
) -> Policy:
    # 每次读取磁盘目录；另一 CLI 改为只读或撤销记住的批准后，本进程不能继续用旧缓存。
    item = await app.runtime.get_thread(thread_id)
    chosen = (item or {}).get("trust_level") or trust_level or app.trust_level
    policy = app._policy_for_thread(thread_id, chosen)
    policy.trust_level = normalize_trust(chosen)
    policy.session_grants = set(item.get("session_grants", [])) if item is not None else set()
    return policy


async def _save_thread_policy(
    app: SayacodeApp,
    thread_id: str,
    *,
    trust_level: str | None = None,
    add_grants: Sequence[str] = (),
    clear_grants: bool = False,
) -> None:
    # 仅提交本次操作的字段；增加批准时与磁盘当前集合合并，清除时明确写空集合。
    if trust_level is None and not add_grants and not clear_grants:
        return

    def changes(current: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if trust_level is not None:
            patch["trust_level"] = normalize_trust(trust_level)
        if clear_grants:
            patch["session_grants"] = []
        elif add_grants:
            patch["session_grants"] = sorted(
                set(current.get("session_grants", [])) | set(add_grants)
            )
        return patch

    saved = await app.runtime.update_thread(thread_id, changes)
    policy = app._policy_for_thread(thread_id)
    policy.trust_level = normalize_trust(saved.get("trust_level"))
    policy.session_grants = set(saved.get("session_grants", []))


def _context(
    app: SayacodeApp,
    thread_id: str,
    trust_level: str,
    *,
    workspace: Path | None = None,
    task_id: str | None = None,
    agent_role: AgentRole = "main",
    background: bool = False,
    profile_name: str | None = None,
) -> AgentContext:
    # 组装一次运行的上下文，把会话档位工作区和输出上限收在一起，后台任务和指定档案按需覆盖。
    active_workspace = (workspace or app.workspace).resolve()
    memory_settings = getattr(app.config, "memory", None)
    memory = getattr(app, "memory", None)
    owner_id = ""
    project_id = ""
    if memory is not None and bool(getattr(memory_settings, "enabled", False)):
        user_scope, project_scope = memory.repository.scopes_for(
            active_workspace,
            parent_workspace=app.workspace if task_id is not None else None,
        )
        owner_id = user_scope.identity
        project_id = project_scope.identity
    return AgentContext(
        workspace=active_workspace,
        trust_level=normalize_trust(trust_level),
        policy=app._policy_for_thread(thread_id, trust_level),
        output_dir=app.paths.outputs,
        session_id=thread_id,
        task_id=task_id,
        agent_role=agent_role,
        profile_name=profile_name or app.profile_name,
        is_background=background,
        output_limit_bytes=app._output_limit_bytes(),
        memory_owner_id=owner_id,
        memory_project_id=project_id,
        memory_use_enabled=bool(memory_settings and memory_settings.enabled and memory_settings.use),
        memory_learning_mode=(
            memory_settings.learn if memory_settings is not None and memory_settings.enabled else "off"
        ),
        memory_learning_enabled=bool(
            memory_settings is not None
            and memory_settings.enabled
            and memory_settings.learn == "auto"
        ),
    )


async def _set_active_session(app: SayacodeApp, session_id: str) -> None:
    await app.runtime.store.aput(
        ("active_sessions",),
        _workspace_key(app.workspace),
        {"workspace": str(app.workspace), "thread_id": session_id, "updated_at": _now()},
        index=False,
    )


async def _new_session(app: SayacodeApp, title: str | None = None) -> str:
    session_id = f"session-{uuid4().hex[:12]}"
    await app._set_active_session(session_id)
    app.trust_level = normalize_trust(app.config.default_trust)
    await app._ensure_thread(session_id, app.trust_level)
    app.session_id = session_id
    if title:
        await app.runtime.update_thread(session_id, {"title": title})
    return session_id


async def _context_for_thread(app: SayacodeApp, thread_id: str) -> tuple[AgentHandle, AgentContext]:
    metadata = await app.runtime.get_thread(thread_id)
    if metadata is None:
        raise KeyError(f"Unknown thread: {thread_id}")
    return await app._get_handle(
        thread_id=thread_id,
        trust_level=str(metadata.get("trust_level") or app.trust_level),
        workspace=Path(metadata.get("workspace") or app.workspace),
        task_id=metadata.get("task_id"),
        agent_role=normalize_agent_role(str(metadata.get("agent_role") or "main")),
        background=bool(metadata.get("is_background")),
        include_team_tools=not bool(metadata.get("is_background")),
    )


async def pending_approval(app: SayacodeApp, thread_id: str | None = None) -> dict[str, Any]:
    """查看父级原生审批中断。供终端审批用。传入应用和会话号，缺省看当前会话。返回会话号加暂停或空闲状态加待批动作加当前档位。只看不改，中断内容以运行时快照为准。"""
    selected = thread_id or app.session_id
    handle, _ = await app._context_for_thread(selected)
    snapshot = await app.runtime.get_state(handle, selected)
    actions = action_requests(list(snapshot.interrupts))
    return {
        "thread_id": selected,
        "status": "paused" if actions else "idle",
        "action_requests": actions,
        "trust_level": (await app._load_thread_policy(selected)).trust_level,
    }


async def _resume_approval(app: SayacodeApp, command: str, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("Approval payload must be an object")
    thread_id = str(args.get("thread_id") or app.session_id)
    async with app._thread_lock(thread_id):
        outcome = await app._resume_approval_unlocked(command, args, thread_id)
    if outcome.get("status") == "completed":
        await app._schedule_pending_wakes(thread_id)
    return outcome


async def _resume_approval_unlocked(
    app: SayacodeApp, command: str, args: dict[str, Any], thread_id: str
) -> dict[str, Any]:
    # 恢复前的校验与流式 Web 恢复共用，确保两种呈现方式只有一套审批语义。
    handle, context, decisions, validated_grants = await _prepare_approval(
        app, command, args, thread_id
    )
    result = await app.runtime.invoke(
        handle,
        context,
        thread_id=thread_id,
        resume={"decisions": decisions},
        callbacks=[app._audit_callback(thread_id, context.task_id)],
    )
    await _remember_grants(app, thread_id, context, validated_grants)
    if result.interrupts:
        return {
            "ok": False,
            "status": "paused",
            "thread_id": thread_id,
            "interrupts": result.interrupts,
            "action_requests": action_requests(list(result.interrupts)),
            "trust_level": context.trust_level,
        }
    response = _final_text(result)
    await _finish_approval(app, handle, context, thread_id, response)
    return {"ok": True, "status": "completed", "thread_id": thread_id, "response": response}


async def _prepare_approval(
    app: SayacodeApp, command: str, args: dict[str, Any], thread_id: str
) -> tuple[AgentHandle, AgentContext, list[dict[str, str]], list[tuple[str, dict[str, Any]]]]:
    """验证原生中断与每一项决定，返回可直接交给 Command(resume) 的载荷。"""
    decisions = args.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("Approval requires at least one decision")
    if not all(isinstance(item, dict) and item.get("type") in {"approve", "reject"} for item in decisions):
        raise ValueError("Approval decisions must be approve or reject objects")
    if command == "reject":
        decisions = [
            item
            if isinstance(item, dict) and item.get("type") == "reject"
            else {"type": "reject", "message": "Declined by user"}
            for item in decisions
        ]
    handle, context = await app._context_for_thread(thread_id)
    snapshot = await app.runtime.get_state(handle, thread_id)
    actions = action_requests(list(snapshot.interrupts))
    if len(actions) != len(decisions):
        raise ValueError("Approval decisions do not match pending actions")
    grants = args.get("grants", [])
    if not isinstance(grants, list):
        raise ValueError("Approval grants must be a list")
    validated_grants: list[tuple[str, dict[str, Any]]] = []
    for grant in grants:
        if not isinstance(grant, dict):
            raise ValueError("Invalid approval grant")
        index = grant.get("index")
        if not isinstance(index, int) or not 0 <= index < len(actions):
            raise ValueError("Approval grant index is out of range")
        if context.policy.trust_level != "ask":
            raise ValueError("Only ask trust can remember an approved call")
        if decisions[index].get("type") != "approve":
            raise ValueError("Cannot grant a rejected action")
        action = actions[index]
        name = str(action.get("name") or "")
        if not name or name != grant.get("tool_name"):
            raise ValueError("Approval grant tool does not match pending action")
        arguments = action.get("args", {})
        if not isinstance(arguments, dict):
            raise ValueError("Approval action arguments must be an object")
        validated_grants.append((name, arguments))
    return handle, context, decisions, validated_grants


async def _remember_grants(
    app: SayacodeApp,
    thread_id: str,
    context: AgentContext,
    validated_grants: list[tuple[str, dict[str, Any]]],
) -> None:
    policy = await app._load_thread_policy(thread_id)
    grant_keys = [policy.grant_call(name, arguments, context) for name, arguments in validated_grants]
    if grant_keys:
        await app._save_thread_policy(thread_id, add_grants=grant_keys)


async def _finish_approval(
    app: SayacodeApp,
    handle: AgentHandle,
    context: AgentContext,
    thread_id: str,
    response: str,
) -> None:
    await app.audit.append(
        "run.resumed", thread_id=thread_id, details={"response_chars": len(response)}
    )
    await app._finalize_memory(handle, context)
    task = await app._task_by_thread(thread_id)
    if task is not None and task.status == "paused":
        task.status = "idle"
        task.last_outcome = "completed"
        task.result = response
        task.turn_seq += 1
        await app.tasks.update(task)


async def stream_approval(
    app: SayacodeApp,
    thread_id: str,
    decisions: list[dict[str, str]],
    *,
    grants: list[dict[str, Any]] | None = None,
    control: RunControl | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """用同一原生检查点流式恢复审批，让工具与模型进度实时进入 Web。"""
    async with app._thread_lock(thread_id):
        handle, context, selected, validated_grants = await _prepare_approval(
            app,
            "approve",
            {"thread_id": thread_id, "decisions": decisions, "grants": grants or []},
            thread_id,
        )
        run = await app.runtime.open_event_stream_v3(
            handle,
            context,
            thread_id=thread_id,
            resume={"decisions": selected},
            control=control,
            callbacks=[app._audit_callback(thread_id, context.task_id)],
        )
        async with run:
            async for event in run:
                for public in app.events.normalize(event, thread_id):
                    yield public
            interrupted = await run.interrupted()
            final = await run.output()
            interrupts = await run.interrupts()
        await _remember_grants(app, thread_id, context, validated_grants)
        if interrupted or interrupts:
            yield {
                "type": "approval.requested",
                "thread_id": thread_id,
                "action_requests": action_requests(list(interrupts)),
                "trust_level": context.trust_level,
            }
            yield {"type": "run.paused", "thread_id": thread_id, "ok": False}
        else:
            response = _final_text(final or {})
            await _finish_approval(app, handle, context, thread_id, response)
            yield {
                "type": "run.completed",
                "thread_id": thread_id,
                "response": response,
                "ok": True,
            }
    await app._schedule_pending_wakes(thread_id)


async def _history(app: SayacodeApp) -> list[dict[str, Any]]:
    # 读当前会话消息史，只做展示投影，不改状态，消息正文以运行时为准。
    handle, _ = await app._context_for_thread(app.session_id)
    state = await app.runtime.get_state(handle, app.session_id)
    messages = state.values.get("messages", []) if state.values else []
    return [
        {
            "id": getattr(message, "id", None),
            "role": (
                "agent_inbox"
                if getattr(message, "additional_kwargs", {}).get("sayacode_source")
                == "agent_inbox"
                else getattr(message, "type", type(message).__name__)
            ),
            "content": _message_text(message),
            "status": getattr(message, "status", None),
        }
        for message in messages
    ]
