"""会话与终端工具类 slash command。

覆盖 help、guide、clear、compact、history、context 与 quit，
核心函数为 print_recent_history，经 router 由交互循环分发调用。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import (
    SayacodeColors,
    console,
    print_agent_message,
    print_banner,
    print_feature_guide,
    print_help,
    print_info,
    print_logo,
    print_success,
    print_user_message,
)
from .base import CommandContext, CommandHandler


@dataclass
class HelpCommandHandler(CommandHandler):
    """展示帮助信息（`/help`）。"""

    name: str = "help"
    aliases: tuple[str, ...] = ("h", "-h")

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        print_help()
        return True


@dataclass
class GuideCommandHandler(CommandHandler):
    """展示功能引导（`/guide`）。"""

    name: str = "guide"
    aliases: tuple[str, ...] = ("tips", "start")

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        print_feature_guide()
        return True


@dataclass
class ClearCommandHandler(CommandHandler):
    """清屏并重绘 Logo（`/clear`）。"""

    name: str = "clear"
    aliases: tuple[str, ...] = ("cls",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        console.clear()
        print_logo(show_full=False)
        return True


@dataclass
class CompactCommandHandler(CommandHandler):
    """压缩会话上下文（`/compact`）。"""

    name: str = "compact"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        agent = runtime.agent
        focus = command.args.strip() or None
        agent.session.compact(focus=focus)
        # 手动压缩后镜像已重写，图状态必须同步覆盖，否则下轮增量仍带旧历史。
        try:
            runner = getattr(agent, "runner", None)
            if runner is not None and getattr(runner, "graph_enabled", False):
                refresh = getattr(agent, "_refresh_turn_prompt", None)
                system_text = refresh() if callable(refresh) else ""
                from langchain_core.messages import SystemMessage

                from ..agent_assembly import history_messages

                runner.sync_messages(
                    [SystemMessage(content=system_text), *history_messages(agent.session)]
                )
        except Exception:
            pass
        info = agent.session.get_compact_info()
        print_success(tr("compact.done"))
        print_info(
            tr(
                "compact.info",
                ratio=_format_context_usage_ratio(info),
                tokens=info["running_tokens"],
                limit=_format_context_limit_from_info(info),
            )
        )
        if info.get("last_compact_time"):
            print_info(tr("compact.last_time", time=info["last_compact_time"]))
        return True


@dataclass
class HistoryCommandHandler(CommandHandler):
    """展示最近历史消息（`/history`）。"""

    name: str = "history"
    aliases: tuple[str, ...] = ("hist",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        print_recent_history(runtime.app_state)
        return True


@dataclass
class ContextCommandHandler(CommandHandler):
    """展示当前上下文摘要（`/context`）。"""

    name: str = "context"
    aliases: tuple[str, ...] = ("proj",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        console.print()
        console.print(f"[{SayacodeColors.PRIMARY}]{tr('context.title')}[/]")
        console.print(runtime.agent.get_context_summary())
        console.print()
        return True


@dataclass
class QuitCommandHandler(CommandHandler):
    """退出交互循环（`/quit`）。"""

    name: str = "quit"
    aliases: tuple[str, ...] = ("exit", "q")

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        return False


def print_recent_history(state: object, limit: int = 12) -> None:
    """渲染最近 user/assistant 消息。"""
    transcript = [msg for msg in state.session.messages if msg.role in {"user", "assistant"}]

    if not transcript:
        print_info(tr("history.none"))
        return

    selected = transcript[-limit:]
    print_banner(tr("history.title"), tr("common.messages", count=len(selected)))

    for message in selected:
        if message.role == "user":
            print_user_message(message.content)
        else:
            print_agent_message(message.content)


def _format_context_usage_ratio(compact_info: dict) -> str:
    if not compact_info.get("context_limit_known"):
        return tr("common.not_set")
    return f"{compact_info['usage_ratio']:.0%}"


def _format_context_limit_from_info(compact_info: dict) -> str:
    limit = compact_info.get("model_context_limit")
    return f"{limit:,}" if compact_info.get("context_limit_known") else tr("common.not_set")


__all__ = [
    "ClearCommandHandler",
    "CompactCommandHandler",
    "ContextCommandHandler",
    "GuideCommandHandler",
    "HelpCommandHandler",
    "HistoryCommandHandler",
    "QuitCommandHandler",
    "print_recent_history",
]
