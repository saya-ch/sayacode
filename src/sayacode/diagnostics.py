"""状态、支持包和只读斜杠工具查询。"""

from __future__ import annotations

import inspect
import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.tools import ToolRuntime

from .tools import build_tools

if TYPE_CHECKING:
    from .application import SayacodeApp


async def _status(app: SayacodeApp) -> dict[str, Any]:
    threads = await app.runtime.list_threads(workspace=app.workspace)
    current = await app.runtime.get_thread(app.session_id)
    active_tasks = [
        record.to_dict()
        for record in await app.tasks.list(workspace=app.workspace)
        if record.status in {"pending", "running", "stopping", "paused"}
    ]
    messages: list[Any] = []
    try:
        handle, _ = await app._context_for_thread(app.session_id)
        state = await app.runtime.get_state(handle, app.session_id)
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
        "workspace": str(app.workspace),
        "session_id": app.session_id,
        "trust_level": app.trust_level,
        "profile": app.profile_name,
        "model": app.model,
        "protocol": app.protocol,
        "base_url": app._profile().base_url if app.model is not None else None,
        "context_length": app._profile().context_length if app.model is not None else None,
        "max_output_tokens": app._profile().max_output_tokens if app.model is not None else None,
        "thread": current,
        "sessions": len([item for item in threads if not item.get("is_background")]),
        "active_tasks": active_tasks,
        "mcp_tools": [item.name for item in app.mcp.tools],
        "mcp_error": app.mcp.error,
        "message_count": len(messages),
        "usage": usage if has_usage else None,
    }


async def _doctor(app: SayacodeApp, bundle: Any = "") -> dict[str, Any]:
    import shutil
    import sys

    checks = {
        "python": sys.version.split()[0],
        "workspace_exists": app.workspace.is_dir(),
        "git": shutil.which("git") is not None,
        "powershell": shutil.which("pwsh") is not None or shutil.which("powershell") is not None,
        "profile_configured": app.profile_name is not None
        or app.config.default_profile is not None,
        "mcp": app.mcp.error is None,
        "checkpoints": app.paths.checkpoints.exists(),
        "store": app.paths.store.exists(),
    }
    result = {"ok": all(checks.values()), "checks": checks, "mcp_error": app.mcp.error}
    target = Path(str(bundle)).expanduser() if str(bundle).strip() else None
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    **result,
                    "workspace": str(app.workspace),
                    "profile": app.profile_name,
                    "recent_audit": await app.audit.list(thread_id=app.session_id, limit=20),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        result["bundle"] = str(target.resolve())
    return result


async def _git_command(app: SayacodeApp, args: Any) -> Any:
    tokens = shlex.split(str(args or ""))
    action = tokens[0] if tokens else "status"
    if action not in {"status", "diff", "log", "branch", "remote", "show"}:
        return {
            "ok": False,
            "action": "ask",
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
    return await app._invoke_native_tool("git", **options)


async def _invoke_native_tool(app: SayacodeApp, tool_name: str, **arguments: Any) -> Any:
    """运行只读斜杠命令助手。走同样策略和上下文。"""
    context = app._context(app.session_id, app.trust_level)
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
        store=app.runtime.store,
        tools=list(tools.values()),
    )
    fn = getattr(selected, "coroutine", None) or getattr(selected, "func", None)
    if not callable(fn):
        raise RuntimeError(f"Tool has no callable implementation: {tool_name}")
    result = fn(runtime=runtime, **arguments)
    return await result if inspect.isawaitable(result) else result
