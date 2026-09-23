"""无交互的 text、json 与 jsonl 运行入口。"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from typing import Any

from .approvals import _TEAM_APPROVAL, _pending_team_approval
from .commands import format_result
from .events import (
    JsonlWriter,
    _exit_code,
    _public_event,
    _redact,
    _response_text,
    _terminal_type,
    _with_task_outcome,
)


async def _wait_for_tasks(app: Any) -> list[dict[str, Any]]:
    """等后台任务收尾并取回任务记录快照。
    参数是应用对象，返回任务字典列表。
    无等待能力返回空列表，返回形状不对时抛错。"""
    waiter = getattr(app, "wait_for_tasks", None)
    if not callable(waiter):
        return []
    value = waiter()
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, list):
        raise TypeError("wait_for_tasks must return a list of task records")
    return [dict(item) for item in value if isinstance(item, dict)]


async def _headless(app: Any, args: argparse.Namespace) -> int:
    """跑一次无交互任务并按指定格式输出，返回进程退出码。
    参数是应用对象与命令行参数，返回零成功一失败三需审批。
    约束是审批类输入直接输出待批准载荷，短横线提示词从标准输入读。
    流程分四段，先拦截后台审批请求，再走事件流式输出，接着走单次运行输出，最后统一收尾异常。
    文本成功走标准输出，失败走标准错误，结构化输出先脱敏。
    坑点是流中断无终止事件时要补失败载荷，任务收尾要合并进最终状态。"""
    output_format = args.output_format
    try:
        prompt = sys.stdin.read().strip() if args.prompt == "-" else str(args.prompt).strip()
        if not prompt:
            raise ValueError("Prompt must not be empty")
        team_approval = _TEAM_APPROVAL.fullmatch(prompt)
        # 审批查询不执行模型，直接输出待批准载荷并返回需审批码。
        if team_approval is not None:
            _, task_id = team_approval.groups()
            pending = await _pending_team_approval(app, task_id)
            payload = {
                "ok": False,
                "status": "paused",
                "thread_id": pending.get("thread_id"),
                "task_id": task_id,
                "action_requests": pending["action_requests"],
                "error": "Interactive approval is required",
            }
            if output_format == "jsonl":
                writer = JsonlWriter(sys.stdout)
                writer.emit({"type": "run.started", "thread_id": pending.get("thread_id")})
                writer.emit({"type": "approval.requested", **payload})
                writer.emit({"type": "run.paused", **payload})
            elif output_format == "json":
                print(json.dumps(_redact(payload), ensure_ascii=False, default=str))
            else:
                print(format_result(_redact(payload)), file=sys.stderr)
            return 3
        if getattr(args, "skill", None):
            try:
                await app.activate_skill(str(args.skill))
            except (KeyError, ValueError) as exc:
                payload = {
                    "ok": False,
                    "status": "config_error",
                    "error": str(exc.args[0] if exc.args else exc),
                    "error_type": type(exc).__name__,
                }
                if output_format == "jsonl":
                    JsonlWriter(sys.stdout).emit({"type": "run.failed", **payload})
                elif output_format == "json":
                    print(json.dumps(_redact(payload), ensure_ascii=False))
                else:
                    print(f"Skill error: {payload['error']}", file=sys.stderr)
                return 2
        if output_format == "jsonl":
            writer = JsonlWriter(sys.stdout)
            # 先发启动事件占住序号，后续事件按流顺序追加。
            writer.emit(
                {
                    "type": "run.started",
                    "session_id": getattr(app, "session_id", None),
                    "workspace": str(args.workspace),
                }
            )
            if args.no_stream:
                result = await app.run(prompt, session_id=args.session, input_format="headless")
                payload = (
                    dict(result)
                    if isinstance(result, dict)
                    else {"ok": True, "response": str(result)}
                )
                payload = _with_task_outcome(payload, await _wait_for_tasks(app))
                drainer = getattr(app, "drain_notifications", None)
                if callable(drainer):
                    for event in drainer():
                        if isinstance(event, dict) and event.get("type") == "review.decision":
                            writer.emit(event)
                for wake in payload.get("parent_wakes", []):
                    writer.emit(wake)
                writer.emit({"type": _terminal_type(payload), **payload})
                return _exit_code(payload)
            terminal: dict[str, Any] | None = None
            response_parts: list[str] = []
            emitted_task_events: set[tuple[str, str]] = set()
            observed_tasks: dict[str, dict[str, Any]] = {}
            stream = app.stream(prompt, session_id=args.session, input_format="headless")
            if inspect.isawaitable(stream):
                stream = await stream
            try:
                async for event in stream:
                    if not isinstance(event, dict):
                        event = {"type": "assistant.delta", "delta": str(event)}
                    public = _public_event(event)
                    if public["type"] == "assistant.delta":
                        response_parts.append(str(public.get("delta") or ""))
                    if public["type"].startswith("task."):
                        task_id = str(public.get("task_id") or "")
                        emitted_task_events.add((task_id, public["type"]))
                        if task_id:
                            observed_tasks[task_id] = {
                                "task_id": task_id,
                                "status": str(
                                    public.get("status") or public["type"].removeprefix("task.")
                                ),
                                "error": public.get("error"),
                            }
                    if public["type"] in {"run.completed", "run.failed", "run.paused"}:
                        terminal = public
                    else:
                        writer.emit(public)
            finally:
                closer = getattr(stream, "aclose", None)
                if callable(closer):
                    await closer()
            tasks = await _wait_for_tasks(app)
            returned_ids = {str(task.get("task_id") or "") for task in tasks}
            tasks.extend(
                task for task_id, task in observed_tasks.items() if task_id not in returned_ids
            )
            for task in tasks:
                status = str(task.get("status") or "unknown")
                kind = f"task.{status}"
                identity = (str(task.get("task_id") or ""), kind)
                if identity not in emitted_task_events:
                    writer.emit(
                        {
                            "type": kind,
                            "task_id": task.get("task_id"),
                            "status": status,
                            "error": task.get("error"),
                        }
                    )
                wake = task.get("parent_wake")
                if isinstance(wake, dict):
                    writer.emit(wake)
            if terminal is None:
                payload = {
                    "ok": False,
                    "status": "failed",
                    "error": "Stream ended without a terminal event",
                    "partial_response": "".join(response_parts),
                }
            else:
                payload = {key: value for key, value in terminal.items() if key != "type"}
                payload.setdefault("status", terminal["type"].removeprefix("run."))
                payload.setdefault("response", "".join(response_parts))
            payload = _with_task_outcome(payload, tasks)
            writer.emit({"type": _terminal_type(payload), **payload})
            return _exit_code(payload)
        result = await app.run(prompt, session_id=args.session, input_format="headless")
        payload = (
            dict(result) if isinstance(result, dict) else {"ok": True, "response": str(result)}
        )
        payload = _with_task_outcome(payload, await _wait_for_tasks(app))
        code = _exit_code(payload)
        if output_format == "json":
            payload.setdefault("ok", code == 0)
            print(json.dumps(_redact(payload), ensure_ascii=False, default=str))
        elif code == 0:
            print(_response_text(payload))
        else:
            print(_response_text(payload) or format_result(payload), file=sys.stderr)
        return code
    except Exception as exc:
        payload = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        if output_format == "jsonl":
            if "writer" not in locals():
                writer = JsonlWriter(sys.stdout)
            writer.emit({"type": "run.failed", **payload})
        elif output_format == "json":
            print(json.dumps(_redact(payload), ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
