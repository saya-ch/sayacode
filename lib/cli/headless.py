"""面向脚本与 CI 的非交互式一次性执行。

解析字面量 prompt 或 stdin 输入，核心函数为 resolve_headless_prompt 与
run_headless，经 StartupService 装配后运行隔离 turn 并输出 text、json 或 jsonl。
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import time
from typing import Any

from lib.api_config import APIConfigManager
from lib.cli.configure import resolve_launch_model_config
from lib.cli.permissions import build_deny_interrupt_handler, configure_permission_confirmation
from lib.runtime import persist_local_state
from lib.runtime.events import JsonlEventWriter, extract_public_tool_events, public_event_identity
from lib.runtime.startup import StartupOptions, StartupService


def resolve_headless_prompt(raw_prompt: str, *, stdin: Any = None) -> str:
    """解析字面量 prompt，或在使用 ``-`` 时从 stdin 读取。"""
    if raw_prompt != "-":
        prompt = str(raw_prompt or "").strip()
    else:
        stream = stdin if stdin is not None else sys.stdin
        prompt = stream.read().strip()
    if not prompt:
        raise ValueError("Prompt must not be empty")
    return prompt


def _emit_payload(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return
    if payload.get("ok"):
        print(str(payload.get("response") or ""))
    else:
        print(f"Error: {payload.get('error') or 'unknown error'}", file=sys.stderr)


def _stream_jsonl_response(agent: Any, prompt: str, writer: JsonlEventWriter) -> str:
    """运行一次真实 Agent 流式输出，并把它的公开表面转换为 JSONL。"""
    response_parts: list[str] = []
    seen_tool_events: set[str] = set()

    def emit_tool_events(chunk: Any) -> None:
        for event in extract_public_tool_events(chunk):
            identity = public_event_identity(event)
            if identity and identity in seen_tool_events:
                continue
            if identity:
                seen_tool_events.add(identity)
            event_type = str(event.pop("type"))
            writer.emit(event_type, **event)

    for delta in agent.stream_run(
        prompt,
        event_callback=emit_tool_events,
        emit_tool_status=False,
    ):
        text = str(delta or "")
        if not text:
            continue
        response_parts.append(text)
        writer.emit("assistant.delta", delta=text)

    return "".join(response_parts)


_TURN_ERROR_TYPES = {
    "model_error": "ModelError",
    "max_retries": "MaxRetriesExceeded",
    "stream_interrupted": "StreamInterrupted",
    "aborted": "AgentAborted",
}


def _agent_failure_payload(agent: Any, response: str) -> dict[str, Any] | None:
    """将终结态的 Agent turn 状态转换为 headless 失败 payload。"""
    state = getattr(agent, "last_turn_state", None)
    transition = getattr(state, "transition", None)
    transition_value = str(getattr(transition, "value", transition or ""))
    if not transition_value or transition_value == "completed":
        return None

    error_message = str(getattr(state, "error_message", "") or "").strip()
    payload: dict[str, Any] = {
        "ok": False,
        "error": error_message or f"Agent turn ended with transition: {transition_value}",
        "error_type": _TURN_ERROR_TYPES.get(transition_value, "IncompleteAgentTurn"),
        "transition": transition_value,
    }
    normalized_response = str(response or "").strip()
    if normalized_response and not normalized_response.startswith("执行出错:"):
        payload["partial_response"] = response
    return payload


def run_headless(
    args: Any,
    user_config: Any,
    *,
    prompt_style: str,
    agent_mode: str,
) -> int:
    """在不产生交互式提示和 UI 噪声的情况下引导一次隔离的 CLI turn。"""
    startup_result = None
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    output_format = getattr(args, "output_format", "text")
    event_writer = JsonlEventWriter(sys.stdout) if output_format == "jsonl" else None
    started_at = time.perf_counter()
    turn_failure = None

    try:
        prompt = resolve_headless_prompt(args.prompt)
        workspace = (
            Path(args.workspace).expanduser().resolve()
            if getattr(args, "workspace", None)
            else Path.cwd().resolve()
        )
        if not workspace.is_dir():
            raise NotADirectoryError(f"Workspace does not exist or is not a directory: {workspace}")

        # 约束 headless 输出为可解析的 protocol surface。
        # 抑制启动卡片、适配器诊断与工具进度。
        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            api_manager = APIConfigManager()
            model_type, model_name, model_config, active_profile = resolve_launch_model_config(
                args=args,
                user_config=user_config,
                api_manager=api_manager,
                interactive_input=False,
            )

            # 无人值守调用绝不能弹出权限提示。
            # 现有的 allow/deny 策略仍然生效；"ask" 决策按 fail closed 处理。
            configure_permission_confirmation(False)
            startup_result = StartupService(                api_manager=api_manager,
                user_config=user_config,
            ).bootstrap(StartupOptions(
                workspace=workspace,
                model_type=model_type,
                model_name=model_name,
                model_config=model_config,
                active_profile=active_profile,
                prompt_style=prompt_style,
                agent_mode=agent_mode,
                stream_output=False,
                confirm_dangerous=False,
                requested_session_id=getattr(args, "session", None),
                create_new_session=bool(getattr(args, "new_session", False)),
            ))
            # 图中断同样不能弹窗：一律拒绝（fail-closed，与上面同姿态）。
            startup_result.agent.interrupt_handler = build_deny_interrupt_handler()
            session = getattr(startup_result.state, "session", None)
            if event_writer is not None:
                event_writer.emit(
                    "run.started",
                    model_type=model_type,
                    model_name=model_name,
                    session_id=getattr(session, "session_id", None),
                    workspace=str(workspace),
                )
                response = _stream_jsonl_response(startup_result.agent, prompt, event_writer)
            else:
                response = startup_result.agent.run(prompt)
            persist_local_state(startup_result.state, user_config)
            turn_failure = _agent_failure_payload(startup_result.agent, response)

        session = getattr(startup_result.state, "session", None)
        if turn_failure is not None:
            failure_payload = {
                **turn_failure,
                "model_type": model_type,
                "model_name": model_name,
                "session_id": getattr(session, "session_id", None),
            }
            if event_writer is not None:
                event_writer.emit(
                    "run.failed",
                    **failure_payload,
                    duration_ms=round((time.perf_counter() - started_at) * 1000),
                )
            else:
                _emit_payload(failure_payload, output_format)
            return 1

        payload = {
            "ok": True,
            "response": str(response),
            "model_type": model_type,
            "model_name": model_name,
            "session_id": getattr(session, "session_id", None),
        }
        if event_writer is not None:
            event_writer.emit(
                "run.completed",
                **payload,
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
        else:
            _emit_payload(payload, output_format)
        return 0
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }
        if event_writer is not None:
            event_writer.emit(
                "run.failed",
                **payload,
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
        else:
            _emit_payload(payload, output_format)
        return 1
    finally:
        agent = getattr(startup_result, "agent", None)
        if agent is not None and hasattr(agent, "close"):
            try:
                with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                    agent.close()
            except Exception:
                # 执行尽力清理，不得破坏已输出的一次性 stdout protocol。
                pass


__all__ = ["resolve_headless_prompt", "run_headless"]
