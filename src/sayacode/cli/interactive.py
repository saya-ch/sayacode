"""唯一交互输入循环和后台通知呈现。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import locale
import re
import shutil
import sys
import warnings
from html import escape
from pathlib import Path
from time import perf_counter
from typing import Any

from ..extensions.custom_commands import discover_custom_commands
from ..prompts import PromptPreferences
from .approvals import _TEAM_APPROVAL, _pending_team_approval, _resume_approval_from_terminal
from .commands import BUILTIN_COMMANDS, CommandRouter, format_result
from .display import TerminalPresenter
from .events import _public_event, _redact, _response_text, _run_ok
from .input import _terminal_prompt
from .model_setup import _first_profile_wizard, _model_key_wizard
from .preferences import _package_version, _state_home, save_preferences


def _needs_profile_setup(app: Any) -> bool:
    """判断启动时是否要弹首次模型向导。
    参数是应用对象，返回是否缺模型配置。
    取不到模型视为需要，读配置出错也视为需要。"""
    if not hasattr(app, "config"):
        return False
    try:
        return getattr(app, "model", None) is None
    except (KeyError, RuntimeError):
        return True


def _terminal_language(preference: str) -> str:
    """把语言偏好落到终端实际可用的中英之一。
    参数是偏好串，返回中英标识。
    自动档跟随系统区域，其余按偏好直返。"""
    if preference in {"zh", "en"}:
        return preference
    current = (locale.getlocale()[0] or "").lower()
    return "zh" if current.startswith(("zh", "chinese")) else "en"


async def _interactive(app: Any, args: argparse.Namespace, preferences: PromptPreferences) -> int:
    """交互入口包一层警告过滤，再进真正的输入循环。
    参数是应用对象、命令行参数与偏好，返回进程退出码。
    过滤的是已知实验性协议提示，不影响正常报错。"""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"^The v3 streaming protocol on Pregel is experimental\.$",
            category=Warning,
        )
        return await _interactive_body(app, args, preferences)


async def _interactive_body(
    app: Any, args: argparse.Namespace, preferences: PromptPreferences
) -> int:
    """跑起唯一交互输入循环，直到退出或遇到文件结束。
    参数是应用对象、命令行参数与偏好，返回进程退出码。
    约束是密钥输入不进历史，审批提问要保持输入行完整。
    流程分五段，先搭终端展示与补全历史，再做首次模型向导与后台通知挂载，接着处理主审批与后台审批两条快捷入口，然后走斜杠分发与模型流式渲染，最后在暂停时接审批续跑。
    坑点是流事件要先脱敏再展示，中断只提示不退出循环。"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style
    from rich.console import Console

    console = Console(
        width=shutil.get_terminal_size((80, 24)).columns if not sys.stdout.isatty() else None
    )
    language = _terminal_language(preferences.language)
    presenter = TerminalPresenter(console, language=language, redact=_redact)
    history_path = _state_home() / "input_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history = FileHistory(str(history_path))
    append_history = history.append_string

    # 密钥类命令不进历史，防止密钥躺在磁盘历史里。
    def safe_append_history(string: str) -> None:
        """安全追加输入历史，失败静默跳过。"""
        command = string.lstrip().lower()
        if command.startswith(("/config add ", "/model add ", "/mcp add ")):
            return
        if re.match(r"^/(?:model|config)\s+key(?:\s|$)", command):
            return
        append_history(string)

    history.append_string = safe_append_history  # type: ignore[method-assign]

    def completion_words() -> list[str]:
        """返回补全候选词表。"""
        commands = [f"/{name}" for name in BUILTIN_COMMANDS]
        commands.extend(
            (
                "/team list",
                "/team pending",
                "/team approve",
                "/team reject",
                "/session list",
                "/trust read_only",
                "/trust ask",
                "/trust full",
            )
        )
        commands.extend(
            item.invocation
            for item in discover_custom_commands(
                Path(getattr(app, "workspace", args.workspace))
            ).values()
        )
        return sorted(set(commands))

    def toolbar() -> HTML:
        """渲染底部工具栏。"""
        trust = escape(str(getattr(app, "trust_level", "ask")).upper())
        model = escape(str(getattr(app, "model", None) or "—"))
        return HTML(
            f" <b>{trust}</b>  {model}  "
            + ("/help 命令  /quit 退出" if language == "zh" else "/help commands  /quit exit")
        )

    prompt_session: PromptSession[str] = PromptSession(
        history=history,
        completer=WordCompleter(completion_words, sentence=True, ignore_case=True),
        complete_while_typing=True,
        enable_history_search=True,
        bottom_toolbar=toolbar,
        style=Style.from_dict(
            {
                "bottom-toolbar": "bg:#202532 #bfc6d4",
                "prompt": "bold #71d3e8",
            }
        ),
    )
    router = CommandRouter(
        app,
        args.workspace,
        preferences,
        save_preferences=save_preferences,
        hooks=getattr(app, "hooks", None),
    )
    if not args.no_clear and sys.stdout.isatty():
        console.clear()
    presenter.header(
        version=_package_version(),
        workspace=Path(getattr(app, "workspace", args.workspace)).resolve(),
        model=getattr(app, "model", None),
        protocol=getattr(app, "protocol", None),
        trust_level=str(getattr(app, "trust_level", "ask")),
        session_id=str(getattr(app, "session_id", "—")),
    )
    if _needs_profile_setup(app):
        presenter.notice(
            "尚未配置模型，请完成首次设置。"
            if language == "zh"
            else "No model profile is configured. Complete setup.",
            level="warning",
        )
        await _first_profile_wizard(
            app, prompt_session, console, language=language, presenter=presenter
        )
    watcher = getattr(app, "watch_notifications", None)
    # 后台通知挂进输入行渲染，保证提示时输入行不被冲掉。
    if callable(watcher):
        from prompt_toolkit.application import run_in_terminal

        def show_notification(event: dict[str, Any]) -> Any:
            """展示一条后台任务通知。"""
            def render() -> None:
                """重绘当前界面。"""
                if str(event.get("type") or "").startswith("agent.wake."):
                    presenter.agent_event(event)
                else:
                    presenter.task_event(event)

            prompt_app = getattr(prompt_session, "app", None)
            if prompt_app is not None and prompt_app.is_running and prompt_app.context is not None:
                return prompt_app.context.copy().run(run_in_terminal, render)
            render()
            return None

        watcher(show_notification)
    # 主循环逐行读输入，先过审批快捷入口再走分发与模型流。
    while True:
        try:
            line = (await _terminal_prompt(prompt_session, "❯ ")).strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            presenter.notice("已取消输入" if language == "zh" else "Input cancelled")
            continue
        if not line:
            continue
        if line.casefold() == "/model add":
            await _first_profile_wizard(
                app, prompt_session, console, language=language, presenter=presenter
            )
            continue
        if re.match(r"^/model\s+key(?:\s|$)", line, re.I):
            await _model_key_wizard(app, line, language=language, presenter=presenter)
            continue
        if re.fullmatch(r"/(?:approve|reject)(?:\s+\S+)?", line, re.I):
            parts = line.split()
            selected = parts[1] if len(parts) == 2 else getattr(app, "session_id", None)
            try:
                pending = app.pending_approval(selected)
                if inspect.isawaitable(pending):
                    pending = await pending
                if not pending.get("action_requests"):
                    presenter.notice(
                        "主会话没有待批准操作"
                        if language == "zh"
                        else "No pending main-agent approval",
                        level="warning",
                    )
                    continue
                presenter.approval_intro(len(pending["action_requests"]))
                reply = await _resume_approval_from_terminal(
                    app,
                    pending,
                    prompt_session,
                    language=language,
                    reject_all=parts[0].lower() == "/reject",
                    presenter=presenter,
                )
                response = _response_text(reply)
                if response:
                    presenter.write_answer(response)
                    presenter.end_turn()
                elif isinstance(reply, dict) and reply.get("status") == "paused":
                    presenter.notice(
                        "仍有待批准操作" if language == "zh" else "Approval is still pending",
                        level="warning",
                    )
            except Exception as exc:
                presenter.notice(
                    f"审批失败：{exc}" if language == "zh" else f"Approval failed: {exc}",
                    level="error",
                )
            continue
        team_approval = _TEAM_APPROVAL.fullmatch(line)
        if team_approval is not None:
            action, task_id = team_approval.groups()
            try:
                pending = await _pending_team_approval(app, task_id)
                presenter.approval_intro(len(pending["action_requests"]))
                reply = await _resume_approval_from_terminal(
                    app,
                    pending,
                    prompt_session,
                    language=language,
                    reject_all=action.lower() == "reject",
                    presenter=presenter,
                )
                if isinstance(reply, dict) and _run_ok(reply):
                    presenter.notice(
                        "后台任务已继续" if language == "zh" else "Task resumed",
                        level="success",
                    )
                    if response := _response_text(reply):
                        presenter.write_answer(response)
                        presenter.end_turn()
                else:
                    presenter.command_result(line, format_result(reply))
            except Exception as exc:
                presenter.notice(
                    f"审批失败：{exc}" if language == "zh" else f"Approval failed: {exc}",
                    level="error",
                )
            continue
        try:
            command = await router.dispatch(line)
        except Exception as exc:
            presenter.notice(
                f"命令错误：{exc}" if language == "zh" else f"Command error: {exc}",
                level="error",
            )
            continue
        if command.exit:
            return 0
        if command.clear:
            console.clear()
            continue
        if line.split(maxsplit=1)[0].lower() == "/lang":
            language = _terminal_language(preferences.language)
            presenter.zh = language == "zh"
        if command.display:
            presenter.command_result(line, command.display)
        if command.prompt is None:
            continue
        try:
            stream = app.stream(
                command.prompt,
                session_id=getattr(app, "session_id", None),
                input_format="interactive",
            )
            if inspect.isawaitable(stream):
                stream = await stream
            pending_approval: dict[str, Any] | None = None
            paused = False
            printed_text = ""
            tool_started: dict[str, float] = {}
            presenter.start_wait()
            try:
                async for event in stream:
                    if not isinstance(event, dict):
                        event = {"type": "assistant.delta", "delta": str(event)}
                    public = _public_event(event)
                    kind = public["type"]
                    if kind == "assistant.delta":
                        delta = str(public.get("delta") or "")
                        printed_text += delta
                        presenter.write_answer(delta)
                    elif kind == "tool.started":
                        tool_name = str(public.get("tool_name") or "tool")
                        tool_id = str(public.get("tool_call_id") or tool_name)
                        tool_started[tool_id] = perf_counter()
                        presenter.tool_event(tool_name, "started")
                        presenter.start_wait(
                            f"正在执行 {tool_name}…"
                            if language == "zh"
                            else f"Running {tool_name}…"
                        )
                    elif kind == "tool.completed":
                        tool_name = str(public.get("tool_name") or "tool")
                        tool_id = str(public.get("tool_call_id") or tool_name)
                        started = tool_started.pop(tool_id, None)
                        presenter.tool_event(
                            tool_name,
                            "completed",
                            duration=perf_counter() - started if started is not None else None,
                        )
                        presenter.start_wait()
                    elif kind == "tool.failed":
                        tool_name = str(public.get("tool_name") or "tool")
                        tool_id = str(public.get("tool_call_id") or tool_name)
                        started = tool_started.pop(tool_id, None)
                        presenter.tool_event(
                            tool_name,
                            "failed",
                            duration=perf_counter() - started if started is not None else None,
                        )
                        if public.get("error"):
                            presenter.notice(str(public["error"]), level="error")
                        presenter.start_wait()
                    elif kind.startswith("task."):
                        presenter.task_event(public)
                        presenter.start_wait()
                    elif kind.startswith("agent.wake."):
                        presenter.agent_event(public)
                    elif kind == "approval.requested":
                        pending_approval = public
                        actions = public.get("action_requests")
                        presenter.approval_intro(len(actions) if isinstance(actions, list) else 1)
                    elif kind == "run.failed":
                        error = str(public.get("error") or "")
                        presenter.notice(
                            f"运行失败：{error}" if language == "zh" else f"Run failed: {error}",
                            level="error",
                        )
                    elif kind == "run.completed":
                        response = _response_text(public)
                        remaining = (
                            response[len(printed_text) :]
                            if response.startswith(printed_text)
                            else response
                        )
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
                    remaining = (
                        response[len(printed_text) :]
                        if response.startswith(printed_text)
                        else response
                    )
                    if remaining:
                        presenter.write_answer(remaining)
                    if not _run_ok(reply):
                        presenter.notice(
                            f"运行未完成：{reply.get('error') or reply.get('status')}"
                            if language == "zh"
                            else f"Run not completed: {reply.get('error') or reply.get('status')}",
                            level="error",
                        )
                elif reply is not None:
                    presenter.command_result("/approve", format_result(reply))
            elif paused:
                presenter.notice(
                    "运行已暂停" if language == "zh" else "Run paused", level="warning"
                )
            presenter.end_turn()
        except (KeyboardInterrupt, asyncio.CancelledError):
            presenter.notice("已中断" if language == "zh" else "Interrupted", level="warning")
        except Exception as exc:
            presenter.notice(f"错误：{exc}" if language == "zh" else f"Error: {exc}", level="error")
