"""Terminal slash-command dispatch without a second agent runtime."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .custom_commands import discover_custom_commands, expand_custom_command
from .hooks import HookRuntime
from .prompts import STYLES, PromptPreferences, normalize_language, normalize_mode, normalize_style

BUILTIN_COMMANDS = (
    "help",
    "guide",
    "start",
    "prefs",
    "clear",
    "compact",
    "status",
    "history",
    "sessions",
    "session",
    "context",
    "symbols",
    "analyze",
    "workspace",
    "paths",
    "model",
    "settings",
    "commands",
    "permissions",
    "doctor",
    "hooks",
    "mode",
    "reset",
    "git",
    "lang",
    "style",
    "tools",
    "stats",
    "config",
    "mcp",
    "team",
    "trace",
    "plan",
    "rewind",
    "quit",
    "exit",
    "memory",
)


@dataclass(slots=True)
class CommandResult:
    display: str = ""
    prompt: str | None = None
    exit: bool = False
    clear: bool = False


def format_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


class CommandRouter:
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

    def _active_mode(self) -> str:
        return str(getattr(self.app, "mode", self.preferences.mode))

    def _save(self) -> None:
        if self.save_preferences:
            self.save_preferences(self.preferences)

    async def _app_command(self, name: str, args: str) -> str:
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

    def _help(self) -> str:
        if self.preferences.language == "zh":
            return "\n".join(
                [
                    "SAYACODE 命令：",
                    "/help  /status  /doctor  /workspace  /model  /session  /history",
                    "/mode [build|plan|review]  /permissions  /tools  /mcp  /hooks",
                    "/plan  /team  /compact  /rewind  /trace  /git  /symbols",
                    "/lang [auto|zh|en]  /style [名称]  /prefs  /memory  /commands  /quit",
                    "输入 /<名称> 查看或执行命令，输入 /commands 查看 Markdown 命令。",
                ]
            )
        rows = [
            "SAYACODE commands:",
            "/help  /status  /doctor  /workspace  /model  /session  /history",
            "/mode [build|plan|review]  /permissions  /tools  /mcp  /hooks",
            "/plan  /team  /compact  /rewind  /trace  /git  /symbols",
            "/lang [auto|zh|en]  /style [name]  /prefs  /memory  /commands  /quit",
            "Use /<name> for details, or /commands for Markdown commands.",
        ]
        return "\n".join(rows)

    async def dispatch(self, text: str) -> CommandResult:
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
            return CommandResult(display=self._help())
        if name == "prefs":
            return CommandResult(
                display=format_result(
                    {
                        "language": self.preferences.language,
                        "style": self.preferences.style,
                        "mode": self._active_mode(),
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
        if name == "mode":
            if not args:
                return CommandResult(display=f"Mode: {self._active_mode()}")
            mode = normalize_mode(args)
            result = await self._app_command("mode", mode)
            self.preferences.mode = mode
            self._save()
            return CommandResult(display=result or f"Mode: {self.preferences.mode}")
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
