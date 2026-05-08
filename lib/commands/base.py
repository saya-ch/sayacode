"""Stable command handler protocol used by CLI command modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..runtime import RuntimeContext


@dataclass(frozen=True)
class CommandContext:
    """One parsed slash command invocation."""

    raw: str
    name: str
    args: str = ""


class CommandHandler(Protocol):
    """Protocol for command handlers moved out of the CLI shell."""

    name: str
    aliases: tuple[str, ...]

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        """Handle a command and return True when it was consumed."""
        ...
