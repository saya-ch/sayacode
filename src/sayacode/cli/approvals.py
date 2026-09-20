"""终端逐项审批及后台任务待批准查询。"""

from __future__ import annotations

import inspect
import re
from typing import Any

from .display import TerminalPresenter
from .input import _terminal_prompt

_TEAM_APPROVAL = re.compile(r"^/team\s+(approve|reject)\s+(\S+)\s*$", re.I)


async def _pending_team_approval(app: Any, task_id: str) -> dict[str, Any]:
    pending = app.command("team", f"pending {task_id}")
    if inspect.isawaitable(pending):
        pending = await pending
    if not isinstance(pending, dict) or pending.get("status") != "paused":
        raise ValueError(f"Task {task_id} has no pending approval")
    if not isinstance(pending.get("action_requests"), list) or not pending["action_requests"]:
        raise ValueError(f"Task {task_id} has no pending actions")
    return pending


async def _resume_approval_from_terminal(
    app: Any,
    pending: dict[str, Any],
    prompt_session: Any,
    *,
    language: str = "auto",
    reject_all: bool = False,
    presenter: TerminalPresenter | None = None,
) -> Any:
    actions = pending.get("action_requests")
    action_requests = actions if isinstance(actions, list) else []
    count = len(action_requests) or 1
    remember_allowed = pending.get("trust_level", getattr(app, "trust_level", "ask")) == "ask"
    decisions: list[dict[str, str]] = []
    grants: list[dict[str, Any]] = []
    for index in range(count):
        action_request = (
            action_requests[index]
            if index < len(action_requests) and isinstance(action_requests[index], dict)
            else {}
        )
        name = str(action_request.get("name") or "tool")
        if presenter is not None:
            presenter.approval_action(index, count, action_request)
        if reject_all:
            answer = "n"
        else:
            label = (
                f"批准 {name} ({index + 1}/{count})？"
                + (
                    "[y 仅本次 / s 记住本次调用 / N 拒绝] "
                    if remember_allowed
                    else "[y 仅本次 / N 拒绝] "
                )
                if language == "zh"
                else f"Approve {name} ({index + 1}/{count})? "
                + ("[y once / s remember exact call / N] " if remember_allowed else "[y once / N] ")
            )
            answer = (await _terminal_prompt(prompt_session, label)).strip().lower()
        approved = answer in ({"y", "yes", "s"} if remember_allowed else {"y", "yes"})
        decisions.append(
            {"type": "approve"}
            if approved
            else {"type": "reject", "message": "Declined in terminal"}
        )
        if approved and answer == "s" and index < len(action_requests):
            tool_name = action_requests[index].get("name")
            if isinstance(tool_name, str) and tool_name:
                grants.append({"index": index, "tool_name": tool_name})
    action = "reject" if all(item["type"] == "reject" for item in decisions) else "approve"
    reply = app.command(
        action,
        {
            "thread_id": pending.get("thread_id") or getattr(app, "session_id", None),
            "decisions": decisions,
            "grants": grants,
        },
    )
    return await reply if inspect.isawaitable(reply) else reply
