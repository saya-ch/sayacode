"""无交互的 text、json 与 jsonl 运行入口。"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from typing import Any

from .events import (
    JsonlWriter,
    _exit_code,
    _public_event,
    _redact,
    _response_text,
    _terminal_type,
    _with_task_outcome,
)


def _format_result(value: Any) -> str:
    """仅供无头 text 模式打印结构化结果。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


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


async def _flush_memory(app: Any, thread_id: Any) -> list[dict[str, Any]]:
    """等本次无交互记忆整理收尾，并取出可公开的简短通知。"""
    memory = getattr(app, "memory", None)
    flush = getattr(memory, "flush_headless", None)
    failures: list[dict[str, Any]] = []
    if callable(flush) and isinstance(thread_id, str) and thread_id:
        try:
            result = flush(thread_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            failures.append(
                {
                    "type": "memory.failed",
                    "thread_id": thread_id,
                    "error_type": type(exc).__name__,
                }
            )
    drainer = getattr(app, "drain_notifications", None)
    if not callable(drainer):
        return failures
    for event in drainer():
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if kind == "review.decision":
            failures.append(event)
        elif (
            kind in {"memory.updated", "memory.failed", "memory.deferred"}
            and event.get("thread_id") == thread_id
        ):
            # 记忆正文、提取输入和模型错误详情不得进入公开事件。
            failures.append(
                {
                    "type": kind,
                    **{
                        key: event[key]
                        for key in (
                            "thread_id",
                            "scope",
                            "source_ref",
                            "count",
                            "job_id",
                            "error_type",
                            "status",
                        )
                        if key in event
                    },
                }
            )
    return failures


async def _headless(app: Any, args: argparse.Namespace) -> int:
    """跑一次无交互任务并按指定格式输出，返回进程退出码。
    参数是应用对象与命令行参数，返回零成功一失败三需审批。
    短横线提示词从标准输入读；审批中断由原生图返回，不解析斜杠文本。
    流程先选择事件流式或单次运行输出，再统一收尾异常。
    文本成功走标准输出，失败走标准错误，结构化输出先脱敏。
    坑点是流中断无终止事件时要补失败载荷，任务收尾要合并进最终状态。"""
    output_format = args.output_format
    try:
        prompt = sys.stdin.read().strip() if args.prompt == "-" else str(args.prompt).strip()
        if not prompt:
            raise ValueError("Prompt must not be empty")
        if getattr(args, "skill", None):
            try:
                await app.activate_skill(str(args.skill))
            except (KeyError, ValueError) as exc:
                payload: dict[str, Any] = {
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
                for event in await _flush_memory(
                    app, payload.get("thread_id") or getattr(app, "session_id", None)
                ):
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
                    if str(public["type"]).startswith("memory."):
                        # 运行中的旧整理通知不属于本次无交互任务。
                        continue
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
            for event in await _flush_memory(
                app, payload.get("thread_id") or getattr(app, "session_id", None)
            ):
                writer.emit(event)
            writer.emit({"type": _terminal_type(payload), **payload})
            return _exit_code(payload)
        result = await app.run(prompt, session_id=args.session, input_format="headless")
        payload = (
            dict(result) if isinstance(result, dict) else {"ok": True, "response": str(result)}
        )
        payload = _with_task_outcome(payload, await _wait_for_tasks(app))
        await _flush_memory(app, payload.get("thread_id") or getattr(app, "session_id", None))
        code = _exit_code(payload)
        if output_format == "json":
            payload.setdefault("ok", code == 0)
            print(json.dumps(_redact(payload), ensure_ascii=False, default=str))
        elif code == 0:
            print(_response_text(payload))
        else:
            print(_response_text(payload) or _format_result(payload), file=sys.stderr)
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
