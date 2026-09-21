"""交互终端中的单轮 Agent 运行与事件投影。"""

from __future__ import annotations

import inspect
from time import perf_counter
from typing import Any

from .approvals import _resume_approval_from_terminal
from .commands import format_result
from .display import TerminalPresenter
from .events import _public_event, _response_text, _run_ok


async def run_interactive_turn(
    app: Any,
    prompt: str,
    prompt_session: Any,
    presenter: TerminalPresenter,
    *,
    language: str,
) -> None:
    """运行一轮模型流，展示工具、任务、审批、正文和执行摘要。"""
    stream = app.stream(
        prompt,
        session_id=getattr(app, "session_id", None),
        input_format="interactive",
    )
    if inspect.isawaitable(stream):
        stream = await stream
    started_at = perf_counter()
    printed_text = ""
    tool_started: dict[str, float] = {}
    tool_inputs: dict[str, Any] = {}
    tool_call_ids: set[str] = set()
    failed_tools = 0
    failed = False
    paused = False
    pending_approval: dict[str, Any] | None = None
    presenter.start_wait()
    try:
        async for event in stream:
            raw = event if isinstance(event, dict) else {"type": "assistant.delta", "delta": str(event)}
            public = _public_event(raw)
            kind = public["type"]
            if kind == "assistant.delta":
                delta = str(public.get("delta") or "")
                printed_text += delta
                presenter.write_answer(delta)
            elif kind == "tool.started":
                tool_name = str(public.get("tool_name") or "tool")
                tool_id = str(public.get("tool_call_id") or tool_name)
                tool_started[tool_id] = perf_counter()
                tool_inputs[tool_id] = public.get("tool_input")
                tool_call_ids.add(tool_id)
                presenter.tool_event(tool_name, "started")
                presenter.start_wait(_running_label(tool_name, language, len(tool_started)))
            elif kind in {"tool.completed", "tool.failed"}:
                tool_name = str(public.get("tool_name") or "tool")
                tool_id = str(public.get("tool_call_id") or tool_name)
                tool_call_ids.add(tool_id)
                tool_at = tool_started.pop(tool_id, None)
                tool_input = tool_inputs.pop(tool_id, None)
                status = "failed" if kind == "tool.failed" else "completed"
                failed_tools += int(status == "failed")
                presenter.tool_event(
                    tool_name,
                    status,
                    duration=perf_counter() - tool_at if tool_at is not None else None,
                    arguments=tool_input,
                    result=public.get("tool_output"),
                )
                if status == "failed" and public.get("error"):
                    presenter.notice(str(public["error"]), level="error")
                presenter.start_wait()
            elif kind.startswith("task."):
                presenter.task_event(public)
                presenter.start_wait()
            elif kind.startswith("agent.wake."):
                presenter.agent_event(public)
            elif kind == "review.decision":
                presenter.review_event(public)
            elif kind == "approval.requested":
                pending_approval = public
                actions = public.get("action_requests")
                presenter.approval_intro(len(actions) if isinstance(actions, list) else 1)
            elif kind == "run.failed":
                failed = True
                error = str(public.get("error") or "")
                presenter.notice(
                    f"运行失败：{error}" if language == "zh" else f"Run failed: {error}",
                    level="error",
                )
            elif kind == "run.completed":
                response = _response_text(public)
                remaining = _remaining_response(response, printed_text)
                if remaining:
                    presenter.write_answer(remaining)
                    printed_text += remaining
            elif kind == "run.paused":
                paused = True
    finally:
        presenter.stop_wait()
        closer = getattr(stream, "aclose", None)
        if callable(closer):
            await closer()

    if paused and pending_approval is not None:
        reply = await _resume_approval_from_terminal(
            app,
            pending_approval,
            prompt_session,
            language=language,
            presenter=presenter,
        )
        if isinstance(reply, dict):
            response = _response_text(reply)
            remaining = _remaining_response(response, printed_text)
            if remaining:
                presenter.write_answer(remaining)
            paused = not _run_ok(reply)
            failed = failed or reply.get("status") in {"failed", "error"}
            if not _run_ok(reply):
                detail = reply.get("error") or reply.get("status")
                presenter.notice(
                    f"运行未完成：{detail}"
                    if language == "zh"
                    else f"Run not completed: {detail}",
                    level="error",
                )
        elif reply is not None:
            presenter.command_result("/approve", format_result(reply))
    elif paused:
        presenter.notice("运行已暂停" if language == "zh" else "Run paused", level="warning")

    presenter.turn_summary(
        duration=perf_counter() - started_at,
        tool_calls=len(tool_call_ids),
        failed_tools=failed_tools,
        paused=paused,
        failed=failed,
    )
    presenter.end_turn()


def _remaining_response(response: str, printed: str) -> str:
    """剔除已经通过增量事件展示过的正文。"""
    return response[len(printed) :] if response.startswith(printed) else response


def _running_label(tool_name: str, language: str, active_count: int) -> str:
    """生成工具运行中的等待文案。"""
    if active_count > 1:
        return (
            f"正在并行执行 {active_count} 个工具…"
            if language == "zh"
            else f"Running {active_count} tools in parallel…"
        )
    return f"正在执行 {tool_name}…" if language == "zh" else f"Running {tool_name}…"


__all__ = ["run_interactive_turn"]
