"""终端斜杠命令分发且不另起运行时。"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..extensions.custom_commands import discover_custom_commands, expand_custom_command
from ..extensions.hooks import HookRuntime
from ..prompts import STYLES, PromptPreferences, normalize_language, normalize_style
from ..tools import tool_catalog
from .help import ALL_COMMAND_NAMES, format_help

BUILTIN_COMMANDS = ALL_COMMAND_NAMES


@dataclass(slots=True)
class CommandResult:
    """斜杠命令分发后的统一结果，外层靠它决定下一步。
    显示文本给终端看，提示文本给模型看，退出与清屏是循环控制信号。"""

    display: str = ""
    prompt: str | None = None
    exit: bool = False
    clear: bool = False


def format_result(value: Any) -> str:
    """把应用层返回值转成终端可显示的文本。
    参数是任意返回值，返回可直接打印的字符串。
    空值给空串，字典列表转多行文本，其余按原样转字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


class CommandRouter:
    """斜杠命令的分发器，复用同一个应用对象不另起运行时。
    参数是应用对象、工作区与偏好，另可带保存回调与钩子运行时。
    约束是密钥类命令只做提示不收参数，真正填写走隐藏输入向导。"""

    def __init__(
        self,
        app: Any,
        workspace: str | Path,
        preferences: PromptPreferences,
        *,
        save_preferences: Callable[[PromptPreferences], None] | None = None,
        hooks: HookRuntime | None = None,
    ) -> None:
        self.app = app
        self.workspace = Path(workspace).expanduser().resolve()
        self.preferences = preferences
        self.save_preferences = save_preferences
        self.hooks = hooks

    def _workspace(self) -> Path:
        return Path(getattr(self.app, "workspace", self.workspace)).expanduser().resolve()

    def _save(self) -> None:
        if self.save_preferences:
            self.save_preferences(self.preferences)

    async def _app_command(self, name: str, args: str) -> str:
        """把斜杠命令转交给应用层同名命令并取回显示文本。
        参数是命令名与原始参数串，返回格式化后的显示文本。
        运行时缺失该命令时返回不可用提示，不抛错。"""
        handler = getattr(self.app, "command", None)
        if not callable(handler):
            return f"/{name} is unavailable in this runtime."
        try:
            value = handler(name, args)
            if inspect.isawaitable(value):
                value = await value
        except (AttributeError, NotImplementedError) as exc:
            return f"/{name} is unavailable: {exc}"
        return format_result(value)

    def _help(self, query: str = "") -> str:
        return format_help(query, language=self.preferences.language)

    async def dispatch(self, text: str) -> CommandResult:
        """解析一行输入并决定是显示、发模型还是退出清屏。
        参数是用户原始输入，返回带显示文本或模型提示的结果对象。
        非斜杠输入原样当任务提示，未知命令给提示不抛错。
        流程分三段，先处理退出清屏帮助等本地命令，再处理偏好与钩子等需落盘的命令，最后透传应用命令或展开自定义命令。
        坑点是模型添加与密钥命令不收行内参数，必须走交互向导。"""
        raw = text.strip()
        if not raw.startswith("/"):
            return CommandResult(prompt=raw)
        token, _, args = raw.partition(" ")
        name = token[1:].lower()
        args = args.strip()
        if name in {"quit", "exit"}:
            return CommandResult(exit=True)
        if name == "clear":
            return CommandResult(clear=True)
        if name in {"help", "guide", "start"}:
            return CommandResult(display=self._help(args))
        if name == "new":
            return CommandResult(display=await self._app_command("session", "new"))
        if name == "models":
            return CommandResult(display=await self._app_command("model", "list"))
        if name in {"model", "config"} and args.casefold().split(maxsplit=1)[0:1] == ["add"]:
            if args.casefold() != "add":
                return CommandResult(
                    display="/model add 不再接收位置参数。请在交互终端单独输入 /model add。"
                    if self.preferences.language == "zh"
                    else "/model add no longer accepts positional arguments. Enter /model add alone in the interactive terminal."
                )
            return CommandResult(
                display="在交互终端输入 /model add，按提示填写接口协议和模型参数。"
                if self.preferences.language == "zh"
                else "Enter /model add in the interactive terminal to configure the API protocol and model."
            )
        if name == "reviewer" and args.casefold().split(maxsplit=1)[0:1] == ["setup"]:
            if args.casefold() != "setup":
                return CommandResult(
                    display="/reviewer setup 不接收位置参数，请按隐藏输入向导配置。"
                    if self.preferences.language == "zh"
                    else "/reviewer setup takes no positional arguments; use the hidden-input wizard."
                )
            return CommandResult(
                display="在交互终端输入 /reviewer setup，按提示配置 Jev。"
                if self.preferences.language == "zh"
                else "Enter /reviewer setup in the interactive terminal to configure Jev."
            )
        if name == "prefs":
            return CommandResult(
                display=format_result(
                    {
                        "language": self.preferences.language,
                        "style": self.preferences.style,
                    }
                )
            )
        if name == "commands":
            found = discover_custom_commands(self._workspace())
            rows = sorted(
                {
                    f"{command.invocation} ({command.scope}) {command.description}".rstrip()
                    for command in found.values()
                }
            )
            return CommandResult(display="\n".join(rows) if rows else "No Markdown commands found.")
        if name == "lang":
            if not args:
                return CommandResult(display=f"Language: {self.preferences.language}")
            language = normalize_language(args)
            await self._app_command("lang", language)
            self.preferences.language = language
            self._save()
            return CommandResult(display=f"Language: {self.preferences.language}")
        if name == "style":
            if not args:
                return CommandResult(display="Styles: " + ", ".join(STYLES))
            style = normalize_style(args)
            await self._app_command("style", style)
            self.preferences.style = style
            self._save()
            return CommandResult(display=f"Style: {self.preferences.style}")
        if name == "hooks" and self.hooks is not None:
            action = args.lower() or "status"
            if action == "trust":
                self.hooks.trust()
            elif action == "untrust":
                self.hooks.untrust()
            elif action == "reload":
                self.hooks.reload()
            elif action == "audit":
                return CommandResult(
                    display=format_result(
                        [
                            {
                                "event": item.event,
                                "name": item.name,
                                "source": item.source,
                                "returncode": item.returncode,
                                "blocked": item.blocked,
                            }
                            for item in self.hooks.results[-20:]
                        ]
                    )
                )
            elif action != "status":
                return CommandResult(display="Usage: /hooks [status|trust|untrust|reload|audit]")
            return CommandResult(display=format_result(self.hooks.status()))
        if name == "sessions":
            return CommandResult(display=await self._app_command("session", args or "list"))
        if name in BUILTIN_COMMANDS:
            return CommandResult(display=await self._app_command(name, args))
        expansion = expand_custom_command(raw, discover_custom_commands(self._workspace()))
        if expansion:
            return CommandResult(prompt=expansion[1])
        return CommandResult(display=f"Unknown command: {token}. Use /help or /commands.")


async def execute_app_command(app: Any, name: str, args: Any = "") -> Any:
    """分发应用级命令，CLI 路由和程序调用共享这一入口。"""
    command = name.lower().strip().lstrip("/")
    if command in {"approve", "reject"}:
        return await app._resume_approval(command, args)
    if command in {"status", "stats", "context"}:
        return await app._status()
    if command == "workspace":
        return str(app.workspace)
    if command == "paths":
        return {
            key: str(getattr(app.paths, key))
            for key in ("home", "config", "checkpoints", "store", "audit", "outputs", "worktrees")
        }
    if command in {"sessions", "session"}:
        return await app._session_command(args)
    if command == "history":
        return await app._history()
    if command == "compact":
        handle, context = await app._get_handle(
            thread_id=app.session_id, trust_level=app.trust_level
        )
        return {
            "compacted": await app.runtime.compact(
                handle,
                context,
                thread_id=app.session_id,
                focus=str(args).strip() or None,
            )
        }
    if command == "rewind":
        return await app._rewind(args)
    if command == "reset":
        return {"session_id": await app._new_session()}
    if command == "trust":
        return await app._trust_command(args)
    if command == "model" and str(args or "").strip() in app.config.profiles:
        return await app._config_command(f"use {str(args).strip()}")
    if command in {"config", "model"}:
        return await app._config_command(args)
    if command == "reviewer":
        return await app._reviewer_command(args)
    if command == "mcp":
        return await app.mcp.command(args)
    if command == "tools":
        context = app._context(app.session_id, app.trust_level)
        catalog = tool_catalog(
            [
                *app._tools_for_context(context),
                *([] if context.trust_level == "read_only" else app.mcp.tools),
            ]
        )
        requested = str(args or "").strip()
        if requested:
            return next(
                (item for item in catalog if item["name"] == requested),
                {"ok": False, "error": f"Unknown tool: {requested}"},
            )
        return catalog
    if command == "todos":
        return await app._todos()
    if command == "team":
        return await app._team_command(args)
    if command == "trace":
        rows = await app.audit.list(thread_id=app.session_id)
        requested = str(args or "").strip()
        return (
            [
                row
                for row in rows
                if row.get("run_id") == requested
                or row.get("details", {}).get("parent_run_id") == requested
            ]
            if requested
            else rows
        )
    if command == "doctor":
        return await app._doctor(args)
    if command == "git":
        return await app._git_command(args)
    if command == "analyze":
        return await app._invoke_native_tool("analyze_project")
    if command == "symbols":
        return await app._invoke_native_tool("list_symbols", query=str(args or "").strip())
    if command == "hooks":
        return app.hooks.status()
    if command == "memory":
        return await app._memory_command(args)
    if command == "lang":
        app.config.preferences["language"] = normalize_language(str(args or "auto"))
        app._handles.clear()
        await app._save_config()
        return {"language": app.config.preferences["language"]}
    if command == "style":
        app.config.preferences["style"] = normalize_style(str(args or "standard"))
        app._handles.clear()
        await app._save_config()
        return {"style": app.config.preferences["style"]}
    if command == "prefs":
        return dict(app.config.preferences)
    if command == "settings":
        return await app._settings_command(args)
    if command == "commands":
        return {"ok": True}
    raise NotImplementedError(f"Unknown command: {name}")
