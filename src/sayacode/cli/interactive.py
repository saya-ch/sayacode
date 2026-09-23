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
from typing import Any

from ..prompts import PromptPreferences
from .approvals import _TEAM_APPROVAL, _pending_team_approval, _resume_approval_from_terminal
from .commands import CommandRouter, format_result
from .completion import SlashCommandCompleter, slash_command_bindings
from .display import TerminalPresenter
from .events import _redact, _response_text, _run_ok
from .input import _terminal_prompt
from .memory import run_memory_menu
from .model_setup import _first_profile_wizard, _model_key_wizard
from .preferences import _package_version, _state_home, save_preferences
from .reviewer import reviewer_setup_wizard
from .theme import Palette
from .turn import run_interactive_turn


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


def _create_prompt_session(
    app: Any,
    presenter: TerminalPresenter,
    completer: SlashCommandCompleter,
) -> Any:
    """创建带安全历史、动态补全和实时状态栏的输入会话。"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style

    history_path = _state_home() / "input_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history = FileHistory(str(history_path))
    append_history = history.append_string

    def safe_append_history(value: str) -> None:
        """跳过可能携带凭据的命令。"""
        command = value.lstrip().lower()
        if command.startswith(("/config add ", "/model add ", "/mcp add ", "/reviewer setup")):
            return
        if re.match(r"^/(?:model|config)\s+key(?:\s|$)", command):
            return
        if re.match(r"^/memory\s+(?:remember|correct)(?:\s|$)", command):
            return
        append_history(value)

    history.append_string = safe_append_history  # type: ignore[assignment]

    def toolbar() -> HTML:
        """按应用当前状态重绘底部状态栏。"""
        trust = escape(str(getattr(app, "trust_level", "ask")).upper())
        model = escape(str(getattr(app, "model", None) or "—"))
        session_id = str(getattr(app, "session_id", "—"))
        session = escape(session_id if len(session_id) <= 20 else "…" + session_id[-19:])
        hint = "/help 命令" if presenter.zh else "/help commands"
        tasks = presenter.task_count()
        task_text = (
            (f"  ·  {tasks} 个后台任务" if presenter.zh else f"  ·  {tasks} background tasks")
            if tasks
            else ""
        )
        completed, total = presenter.todo_progress()
        todo_text = f"  ·  Todo {completed}/{total}" if total else ""
        active, pending = presenter.memory_progress()
        if active:
            memory_text = (
                f"  ·  记忆整理中 {active} / 待处理 {pending}"
                if presenter.zh
                else f"  ·  Memory learning {active} / pending {pending}"
            )
        elif pending:
            memory_text = (
                f"  ·  记忆待处理 {pending}"
                if presenter.zh
                else f"  ·  Memory pending {pending}"
            )
        else:
            memory_text = ""
        return HTML(
            f" <b>{trust}</b>  {model}  ·  {session}{task_text}{todo_text}{memory_text}  {hint}"
        )

    return PromptSession(
        history=history,
        completer=completer,
        key_bindings=slash_command_bindings(),
        complete_while_typing=True,
        enable_history_search=True,
        bottom_toolbar=toolbar,
        style=Style.from_dict(
            {
                "bottom-toolbar": f"bg:{Palette.toolbar_bg} {Palette.toolbar_fg}",
                "prompt": f"bold {Palette.prompt}",
                "completion-menu": f"bg:{Palette.toolbar_bg} {Palette.toolbar_fg}",
                "completion-menu.completion.current": f"bg:{Palette.prompt} #111827 bold",
                "completion-menu.meta.completion.current": f"bg:{Palette.prompt} #111827",
                "completion-menu.meta.completion": f"bg:{Palette.toolbar_bg} #9ca3af",
            }
        ),
    )


async def _refresh_memory_status(app: Any, presenter: TerminalPresenter) -> None:
    """在输入轮次和整理通知后读取一次 Store 状态，不在按键重绘时查询。"""
    status_reader = getattr(getattr(app, "memory", None), "status", None)
    if not callable(status_reader):
        return
    try:
        status = status_reader()
        if inspect.isawaitable(status):
            status = await status
    except Exception:
        return
    if isinstance(status, dict):
        presenter.update_memory_status(status)


def _install_notification_watcher(
    app: Any,
    prompt_session: Any,
    presenter: TerminalPresenter,
) -> None:
    """把后台通知接入 prompt_toolkit 的安全重绘边界。"""
    watcher = getattr(app, "watch_notifications", None)
    if not callable(watcher):
        return
    from prompt_toolkit.application import run_in_terminal

    def show_notification(event: dict[str, Any]) -> Any:
        kind = str(event.get("type") or "")

        def render() -> None:
            if kind == "review.decision":
                presenter.review_event(event)
            elif kind.startswith("agent.wake."):
                presenter.agent_event(event)
            elif kind.startswith("memory."):
                presenter.memory_event(event)
            elif kind.startswith("tool."):
                presenter.tool_event(
                    str(event.get("tool_name") or "tool"),
                    kind.removeprefix("tool."),
                    arguments=event.get("tool_input"),
                    result=event.get("tool_output"),
                    thread_id=str(event.get("thread_id") or ""),
                    role=str(event.get("agent_role") or "main"),
                )
                if kind == "tool.failed" and event.get("error"):
                    presenter.notice(str(event["error"]), level="error")
            else:
                presenter.task_event(event)

        prompt_app = getattr(prompt_session, "app", None)
        if prompt_app is not None and prompt_app.is_running and prompt_app.context is not None:
            rendered = prompt_app.context.copy().run(run_in_terminal, render)
        else:
            render()
            rendered = None
        if not kind.startswith("memory."):
            return rendered

        async def refresh_after_render() -> None:
            if inspect.isawaitable(rendered):
                await rendered
            await _refresh_memory_status(app, presenter)
            if prompt_app is not None and prompt_app.is_running:
                prompt_app.invalidate()

        return refresh_after_render()

    watcher(show_notification)


async def _handle_setup_command(
    app: Any,
    line: str,
    prompt_session: Any,
    presenter: TerminalPresenter,
    language: str,
) -> bool:
    """处理必须使用隐藏输入或分步向导的命令。"""
    if line.casefold() == "/model add":
        await _first_profile_wizard(
            app,
            prompt_session,
            presenter.console,
            language=language,
            presenter=presenter,
        )
        return True
    if line.casefold() == "/reviewer setup":
        await reviewer_setup_wizard(
            app,
            prompt_session,
            language=language,
            presenter=presenter,
        )
        return True
    if re.match(r"^/model\s+key(?:\s|$)", line, re.I):
        await _model_key_wizard(app, line, language=language, presenter=presenter)
        return True
    return False


async def _handle_main_approval(
    app: Any,
    line: str,
    prompt_session: Any,
    presenter: TerminalPresenter,
    language: str,
) -> bool:
    """处理主 Agent 的批准或拒绝快捷命令。"""
    if re.fullmatch(r"/(?:approve|reject)(?:\s+\S+)?", line, re.I) is None:
        return False
    parts = line.split()
    selected = parts[1] if len(parts) == 2 else getattr(app, "session_id", None)
    try:
        pending = app.pending_approval(selected)
        if inspect.isawaitable(pending):
            pending = await pending
        if not pending.get("action_requests"):
            presenter.notice(
                "主会话没有待批准操作" if language == "zh" else "No pending main-agent approval",
                level="warning",
            )
            return True
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
    return True


async def _handle_team_approval(
    app: Any,
    line: str,
    prompt_session: Any,
    presenter: TerminalPresenter,
    language: str,
) -> bool:
    """处理指定后台任务的批准或拒绝快捷命令。"""
    match = _TEAM_APPROVAL.fullmatch(line)
    if match is None:
        return False
    action, task_id = match.groups()
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
    return True


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
    """初始化终端资源并运行轻量输入控制循环。"""
    from rich.console import Console

    console = Console(
        width=shutil.get_terminal_size((80, 24)).columns if not sys.stdout.isatty() else None
    )
    language = _terminal_language(preferences.language)
    presenter = TerminalPresenter(console, language=language, redact=_redact)
    completer = SlashCommandCompleter(app, language=lambda: "zh" if presenter.zh else "en")
    prompt_session = _create_prompt_session(app, presenter, completer)
    router = CommandRouter(
        app,
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
            app,
            prompt_session,
            console,
            language=language,
            presenter=presenter,
        )
    _install_notification_watcher(app, prompt_session, presenter)

    while True:
        try:
            await completer.refresh_directory()
            await _refresh_memory_status(app, presenter)
            line = (await _terminal_prompt(prompt_session, "❯ ")).strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            presenter.notice("已取消输入" if language == "zh" else "Input cancelled")
            continue
        if not line:
            continue
        if await _handle_setup_command(app, line, prompt_session, presenter, language):
            continue
        if await _handle_main_approval(app, line, prompt_session, presenter, language):
            continue
        if await _handle_team_approval(app, line, prompt_session, presenter, language):
            continue
        if line.casefold() == "/memory":
            try:
                if await run_memory_menu(app, prompt_session, presenter, language):
                    continue
            except KeyboardInterrupt:
                presenter.notice("已返回对话" if language == "zh" else "Back to chat")
                continue
            except Exception as exc:
                presenter.notice(
                    f"记忆操作失败：{exc}"
                    if language == "zh"
                    else f"Memory operation failed: {exc}",
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
            await run_interactive_turn(
                app,
                command.prompt,
                prompt_session,
                presenter,
                language=language,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            presenter.notice("已中断" if language == "zh" else "Interrupted", level="warning")
        except Exception as exc:
            presenter.notice(
                f"错误：{exc}" if language == "zh" else f"Error: {exc}",
                level="error",
            )
