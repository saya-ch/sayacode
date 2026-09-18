"""运行时 command router。

负责 slash command 解析与分发，核心类为 CommandRouter，
核心函数为 parse_command 与 normalize_command_name，供交互循环调度调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .base import CommandContext, CommandHandler
from ..runtime import RuntimeContext


@dataclass(frozen=True)
class CommandRoute:
    """一条规范化后的 command route。"""

    name: str
    handler: CommandHandler


class CommandRouter:
    """把 slash command 分发到感知运行时的 handler。"""

    def __init__(self, handlers: Optional[Iterable[CommandHandler]] = None) -> None:
        self._routes: dict[str, CommandHandler] = {}
        for handler in handlers or ():
            self.register(handler)

    def register(self, handler: CommandHandler) -> None:
        """注册 handler 及其别名到路由表。"""
        names = {handler.name, *getattr(handler, "aliases", ())}
        for name in names:
            normalized = normalize_command_name(name)
            if normalized:
                self._routes[normalized] = handler

    def dispatch(self, raw_command: str, runtime: RuntimeContext) -> Optional[bool]:
        """分发命令；非命令或无匹配时返回 None 交给 Agent。"""
        raw = str(raw_command or "")
        if not raw.strip().startswith("/"):
            return None
        command = parse_command(raw_command)
        if command is None:
            return None

        handler = self._routes.get(command.name)
        if handler is None:
            return None
        return handler.handle(command, runtime)

    def list_routes(self) -> list[CommandRoute]:
        """列出全部已注册路由（按名称排序）。"""
        return [
            CommandRoute(name=name, handler=handler)
            for name, handler in sorted(self._routes.items())
        ]


def parse_command(raw_command: str) -> Optional[CommandContext]:
    """解析原始输入为结构化 slash command。"""
    raw = str(raw_command or "").strip()
    if not raw:
        return None
    first, _, args = raw.partition(" ")
    name = normalize_command_name(first)
    if not name:
        return None
    return CommandContext(raw=raw, name=name, args=args.strip())


def normalize_command_name(value: str) -> str:
    """规范化命令名并剥离前导斜杠。"""
    text = str(value or "").strip().lower()
    while text.startswith("/"):
        text = text[1:]
    return text.strip()


__all__ = ["CommandRoute", "CommandRouter", "normalize_command_name", "parse_command"]
