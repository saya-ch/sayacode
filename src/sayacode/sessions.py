"""会话目录、信任档位和原生检查点操作。"""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .agent import AgentContext, AgentHandle
from .agent.events import _final_text, _message_text, action_requests
from .trust import Policy, normalize_trust

if TYPE_CHECKING:
    from .application import SayacodeApp


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _workspace_key(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:24]


async def _ensure_thread(app: SayacodeApp, thread_id: str, trust_level: str) -> None:
    await app._load_thread_policy(thread_id, trust_level=trust_level)
    context = app._context(thread_id, trust_level)
    if await app.runtime.get_thread(thread_id) is None:
        await app.runtime.put_thread(thread_id, context, status="idle", title="New session")


def _policy_for_thread(app: SayacodeApp, thread_id: str, trust_level: str | None = None) -> Policy:
    policy = app._thread_policies.get(thread_id)
    if policy is None:
        policy = Policy(trust_level=normalize_trust(trust_level or app.trust_level))
        app._thread_policies[thread_id] = policy
    return policy


async def _load_thread_policy(
    app: SayacodeApp, thread_id: str, *, trust_level: str | None = None
) -> Policy:
    if thread_id not in app._thread_policies:
        item = await app.runtime.get_thread(thread_id)
        chosen = (item or {}).get("trust_level") or trust_level or app.trust_level
        policy = app._policy_for_thread(thread_id, chosen)
        if item is not None:
            policy.session_grants.update(item.get("session_grants", []))
    return app._thread_policies[thread_id]


async def _save_thread_policy(app: SayacodeApp, thread_id: str) -> None:
    item = await app.runtime.get_thread(thread_id)
    if item is None:
        return
    policy = app._policy_for_thread(thread_id)
    item["trust_level"] = policy.trust_level
    item["session_grants"] = sorted(policy.session_grants)
    item["updated_at"] = _now()
    await app.runtime.store.aput(("threads",), thread_id, item, index=False)


def _context(
    app: SayacodeApp,
    thread_id: str,
    trust_level: str,
    *,
    workspace: Path | None = None,
    task_id: str | None = None,
    background: bool = False,
    profile_name: str | None = None,
) -> AgentContext:
    active_workspace = (workspace or app.workspace).resolve()
    return AgentContext(
        workspace=active_workspace,
        trust_level=normalize_trust(trust_level),
        policy=app._policy_for_thread(thread_id, trust_level),
        output_dir=app.paths.outputs,
        session_id=thread_id,
        task_id=task_id,
        profile_name=profile_name or app.profile_name,
        is_background=background,
        output_limit_bytes=app._output_limit_bytes(),
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
        item = await app.runtime.get_thread(session_id)
        if item is not None:
            item["title"] = title
            item["updated_at"] = _now()
            await app.runtime.store.aput(("threads",), session_id, item, index=False)
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
        background=bool(metadata.get("is_background")),
        include_team_tools=not bool(metadata.get("is_background")),
    )


async def pending_approval(app: SayacodeApp, thread_id: str | None = None) -> dict[str, Any]:
    """查看父级原生审批中断。供终端审批用。"""
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
    paused_events = await app._parent_event_items(thread_id, state="paused")
    if paused_events:
        context = replace(context, task_notification=app._task_notice(dict(paused_events[0].value)))
    result = await app.runtime.invoke(
        handle,
        context,
        thread_id=thread_id,
        resume={"decisions": decisions},
        callbacks=[app._audit_callback(thread_id, context.task_id)],
    )
    policy = await app._load_thread_policy(thread_id)
    for name, arguments in validated_grants:
        policy.grant_call(name, arguments, context)
    if validated_grants:
        await app._save_thread_policy(thread_id)
    if result.interrupts:
        return {
            "ok": False,
            "status": "paused",
            "thread_id": thread_id,
            "interrupts": result.interrupts,
        }
    response = _final_text(result)
    await app.audit.append(
        "run.resumed", thread_id=thread_id, details={"response_chars": len(response)}
    )
    for item in paused_events:
        await app._set_parent_event_state(dict(item.value), "delivered")
    task = await app._task_by_thread(thread_id)
    if task is not None and task.status == "paused":
        task.status = "completed"
        task.result = response
        await app.tasks.update(task)
    return {"ok": True, "status": "completed", "thread_id": thread_id, "response": response}


async def _session_command(app: SayacodeApp, args: Any) -> Any:
    tokens = shlex.split(str(args or ""))
    action = tokens[0].lower() if tokens else "current"
    if action in {"current", "show"}:
        return await app.runtime.get_thread(app.session_id)
    if action in {"list", "sessions"}:
        return [
            item
            for item in await app.runtime.list_threads(workspace=app.workspace)
            if not item.get("is_background")
        ]
    if action in {"new", "create"}:
        return {"session_id": await app._new_session(" ".join(tokens[1:]) or None)}
    if action in {"use", "switch"}:
        if len(tokens) != 2:
            raise ValueError("Usage: /session use <thread-id>")
        thread_id = tokens[1]
        item = await app.runtime.get_thread(thread_id)
        if item is None or Path(item.get("workspace", "")).resolve() != app.workspace:
            raise KeyError(f"Unknown session: {thread_id}")
        app.session_id = thread_id
        app.trust_level = normalize_trust(item.get("trust_level"))
        await app._set_active_session(thread_id)
        await app._schedule_pending_wakes(thread_id)
        return item
    if action == "rename":
        if len(tokens) < 2:
            raise ValueError("Usage: /session rename <title>")
        item = await app.runtime.get_thread(app.session_id)
        if item is None:
            raise KeyError(app.session_id)
        item["title"] = " ".join(tokens[1:])
        item["updated_at"] = _now()
        await app.runtime.store.aput(("threads",), app.session_id, item, index=False)
        return item
    raise ValueError("Usage: /session [current|list|new|use|rename]")


async def _history(app: SayacodeApp) -> list[dict[str, Any]]:
    handle, _ = await app._context_for_thread(app.session_id)
    state = await app.runtime.get_state(handle, app.session_id)
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


async def _rewind(app: SayacodeApp, args: Any) -> Any:
    handle, _ = await app._context_for_thread(app.session_id)
    history = await app.runtime.get_history(handle, app.session_id)
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
    fork = await app.runtime.rewind(handle, app.session_id, checkpoint_id)
    return {"rewound": True, "checkpoint": checkpoint_id, "fork": fork}


async def _trust_command(app: SayacodeApp, args: Any) -> dict[str, Any]:
    tokens = shlex.split(str(args or ""))
    policy = await app._load_thread_policy(app.session_id)
    if not tokens or tokens == ["show"]:
        return {
            "trust_level": policy.trust_level,
            "default_trust": app.config.default_trust,
            "remembered_calls": len(policy.session_grants),
            "shell_sandboxed": False,
        }
    if len(tokens) == 1 and tokens[0] == "clear":
        policy.session_grants.clear()
        await app._save_thread_policy(app.session_id)
        return {"cleared": "session approvals"}
    if len(tokens) == 2 and tokens[0] == "default":
        app.config.default_trust = normalize_trust(tokens[1])
        await app._save_config()
        return {"default_trust": app.config.default_trust}
    if len(tokens) == 1:
        policy.trust_level = normalize_trust(tokens[0])
        app.trust_level = policy.trust_level
        await app._save_thread_policy(app.session_id)
        app._handles.clear()
        return {"trust_level": policy.trust_level}
    raise ValueError("Usage: /trust [read_only|ask|full|default <level>|clear]")


async def _todos(app: SayacodeApp) -> Any:
    handle, _ = await app._context_for_thread(app.session_id)
    state = await app.runtime.get_state(handle, app.session_id)
    return list(state.values.get("todos", [])) if state.values else []
