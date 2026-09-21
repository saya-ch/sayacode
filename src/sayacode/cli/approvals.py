"""终端逐项审批及后台任务待批准查询。"""

from __future__ import annotations

import inspect
import re
from typing import Any

from .display import TerminalPresenter
from .input import _terminal_prompt

# 匹配终端里处理后台任务审批的两种写法，只认批准或拒绝加任务号。
_TEAM_APPROVAL = re.compile(r"^/team\s+(approve|reject)\s+(\S+)\s*$", re.I)


async def _pending_team_approval(app: Any, task_id: str) -> dict[str, Any]:
    """查出后台任务的待批准快照，供终端逐项展示用。
    参数是应用对象与后台任务号，返回暂停态任务字典。
    只接受暂停且带有操作请求的任务，其余情况抛错，不做自动批准。"""
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
    """在终端逐项问完批准决定，再把结果一次发回应用。
    参数是应用对象与待批准快照，另可指定语言与是否直接全拒绝。
    返回应用继续执行的结果，调用方按此决定展示还是报错。
    流程分三段，先逐项展示卡片并提问，再按档位决定是否开放记住选项，最后汇总批准或拒绝并附带记忆授权发回。
    坑点是记住选项只在询问档可用，无头模式不走这里。"""
    actions = pending.get("action_requests")
    action_requests = actions if isinstance(actions, list) else []
    count = len(action_requests) or 1
    remember_allowed = pending.get("trust_level", getattr(app, "trust_level", "ask")) == "ask"
    decisions: list[dict[str, str]] = []
    grants: list[dict[str, Any]] = []
    # 逐项展示卡片再提问，拒绝模式直接记为拒绝不提问。
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
            target = presenter.approval_target(action_request) if presenter is not None else name
            label = (
                f"批准 {target} ({index + 1}/{count})？"
                + (
                    "[y 仅本次 / s 记住本次调用 / N 拒绝] "
                    if remember_allowed
                    else "[y 仅本次 / N 拒绝] "
                )
                if language == "zh"
                else f"Approve {target} ({index + 1}/{count})? "
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
