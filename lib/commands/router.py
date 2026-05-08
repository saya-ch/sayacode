"""Runtime command router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .base import CommandContext, CommandHandler
from ..runtime import RuntimeContext


@dataclass(frozen=True)
class CommandRoute:
    """One normalized command route."""

    name: str
    handler: CommandHandler


class CommandRouter:
    """Dispatch slash commands to runtime-aware handlers."""

    def __init__(self, handlers: Optional[Iterable[CommandHandler]] = None) -> None:
        self._routes: dict[str, CommandHandler] = {}
        for handler in handlers or ():
            self.register(handler)

    def register(self, handler: CommandHandler) -> None:
        names = {handler.name, *getattr(handler, "aliases", ())}
        for name in names:
            normalized = normalize_command_name(name)
            if normalized:
                self._routes[normalized] = handler

    def dispatch(self, raw_command: str, runtime: RuntimeContext) -> Optional[bool]:
        command = parse_command(raw_command)
        if command is None:
            return None

        handler = self._routes.get(command.name)
        if handler is None:
            return None
        return handler.handle(command, runtime)

    def list_routes(self) -> list[CommandRoute]:
        return [
            CommandRoute(name=name, handler=handler)
            for name, handler in sorted(self._routes.items())
        ]


def parse_command(raw_command: str) -> Optional[CommandContext]:
    raw = str(raw_command or "").strip()
    if not raw:
        return None
    first, _, args = raw.partition(" ")
    name = normalize_command_name(first)
    if not name:
        return None
    return CommandContext(raw=raw, name=name, args=args.strip())


def normalize_command_name(value: str) -> str:
    text = str(value or "").strip().lower()
    while text.startswith("/"):
        text = text[1:]
    return text.strip()


__all__ = ["CommandRoute", "CommandRouter", "normalize_command_name", "parse_command"]
