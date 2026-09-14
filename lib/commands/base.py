"""CLI 命令模块共用的稳定命令处理协议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..runtime import RuntimeContext


@dataclass(frozen=True)
class CommandContext:
    """一次已解析的 slash 命令调用。"""

    raw: str
    name: str
    args: str = ""


class CommandHandler(Protocol):
    """从 CLI 外壳中拆分出来的命令处理器协议。"""

    name: str
    aliases: tuple[str, ...]

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        """处理命令，已被消费时返回 True。"""
        ...
