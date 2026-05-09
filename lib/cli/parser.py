"""
CLI 参数解析器

包含命令行参数解析、内置命令列表、协议选择菜单、语言覆盖等。
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from lib.models.provider_catalog import USER_VISIBLE_PROVIDER_TYPES, visible_provider_options
from lib.state import UserConfig
from lib.theme import (
    console,
    SayacodeColors,
)
from lib.i18n import (
    set_language,
    tr,
)
from lib.cli.permissions import _supports_interactive_input


CLI_VERSION = "SAYACODE v1.1.0"

BUILTIN_COMMANDS = [
    "/help",
    "/guide",
    "/start",
    "/prefs",
    "/clear",
    "/compact",
    "/status",
    "/history",
    "/sessions",
    "/session",
    "/session new",
    "/session use",
    "/session list",
    "/session current",
    "/session rename",
    "/context",
    "/symbols",
    "/analyze",
    "/workspace",
    "/paths",
    "/model",
    "/model list",
    "/model use",
    "/model add",
    "/model test",
    "/model show",
    "/settings",
    "/commands",
    "/permissions",
    "/doctor",
    "/hooks",
    "/mode",
    "/reset",
    "/git",
    "/lang",
    "/style",
    "/tools",
    "/stats",
    "/config",
    "/mcp",
    "/quit",
]

PROTOCOL_OPTIONS: List[Dict[str, Any]] = visible_provider_options()

PROTOCOL_DEFAULTS: Dict[str, Dict[str, Any]] = {
    option["value"]: option for option in PROTOCOL_OPTIONS
}
USER_VISIBLE_MODEL_TYPES = list(USER_VISIBLE_PROVIDER_TYPES)


def _protocol_options() -> List[Dict[str, Any]]:
    return visible_provider_options()


def _protocol_defaults() -> Dict[str, Dict[str, Any]]:
    return {option["value"]: option for option in _protocol_options()}


def _read_menu_key() -> str:
    """读取一个菜单按键。"""
    if os.name == "nt":
        import msvcrt

        first = msvcrt.getwch()
        if first in ("\r", "\n"):
            return "enter"
        if first == "\x03":
            raise KeyboardInterrupt
        if first in ("\x00", "\xe0"):
            second = msvcrt.getwch()
            return {"H": "up", "P": "down"}.get(second, "")
        return first

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        first = sys.stdin.read(1)
        if first in ("\r", "\n"):
            return "enter"
        if first == "\x03":
            raise KeyboardInterrupt
        if first == "\x1b":
            second = sys.stdin.read(1)
            third = sys.stdin.read(1)
            if second == "[":
                return {"A": "up", "B": "down"}.get(third, "")
        return first
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _build_protocol_menu(selected_index: int) -> Group:
    """构建接口协议选择菜单。"""
    lines: List[Text] = [
        Text(tr("protocol.menu_hint"), style=SayacodeColors.TEXT_DIM),
        Text(""),
    ]

    protocol_options = _protocol_options()
    for index, option in enumerate(protocol_options):
        is_selected = index == selected_index
        prefix = "›" if is_selected else " "
        style = f"bold {SayacodeColors.PRIMARY}" if is_selected else SayacodeColors.TEXT
        meta_style = SayacodeColors.TEXT_DIM
        row = Text()
        row.append(f"{prefix} ", style=style)
        row.append(option["label"], style=style)
        lines.append(row)
        description = Text(f"  {option.get('description', '')}", style=meta_style)
        lines.append(description)
        if index != len(protocol_options) - 1:
            lines.append(Text(""))

    return Panel(
        Group(*lines),
        title=f"[bold {SayacodeColors.PRIMARY}]{tr('protocol.title')}[/]",
        subtitle=f"[{SayacodeColors.TEXT_DIM}]{tr('protocol.subtitle')}[/]",
        border_style=SayacodeColors.BORDER_BRIGHT,
    )


def select_model_protocol(default_index: int = 3) -> Dict[str, Any]:
    """通过上下键菜单选择模型接入协议。"""
    protocol_options = _protocol_options()
    if not _supports_interactive_input():
        return dict(protocol_options[default_index])

    selected_index = default_index
    console.print()

    with Live(_build_protocol_menu(selected_index), console=console, refresh_per_second=20, transient=True) as live:
        while True:
            key = _read_menu_key()

            if key == "up":
                selected_index = (selected_index - 1) % len(protocol_options)
            elif key == "down":
                selected_index = (selected_index + 1) % len(protocol_options)
            elif key == "enter":
                return dict(protocol_options[selected_index])

            live.update(_build_protocol_menu(selected_index))


def _language_override_from_argv(argv: Optional[List[str]]) -> Optional[str]:
    """Read --lang before argparse handles --help and exits."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    for index, item in enumerate(raw_args):
        if item == "--lang" and index + 1 < len(raw_args):
            return raw_args[index + 1]
        if item.startswith("--lang="):
            return item.split("=", 1)[1]
    return None


def _prepare_cli_language(argv: Optional[List[str]], user_config: UserConfig) -> None:
    requested = _language_override_from_argv(argv)
    set_language(requested or user_config.language)


class LocalizedHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Argparse formatter with localized section headings."""

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(
            usage,
            actions,
            groups,
            prefix if prefix is not None else f"{tr('cli.help.usage')}: ",
        )

    def start_section(self, heading):
        headings = {
            "options": tr("cli.help.options"),
            "positional arguments": tr("cli.help.positionals"),
        }
        return super().start_section(headings.get(heading, heading))


def build_cli_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        description=tr("cli.description"),
        formatter_class=LocalizedHelpFormatter,
        add_help=False,
        epilog=tr("cli.examples"),
    )
    parser.add_argument("-h", "--help", action="help", help=tr("cli.help.help"))
    parser.add_argument("--version", action="version", version=CLI_VERSION, help=tr("cli.help.version"))
    parser.add_argument("--workspace", help=tr("cli.help.workspace"))
    parser.add_argument("--model-type", choices=USER_VISIBLE_MODEL_TYPES, help=tr("cli.help.model_type"))
    parser.add_argument("--model-name", help=tr("cli.help.model_name"))
    parser.add_argument("--base-url", help=tr("cli.help.base_url"))
    parser.add_argument("--api-key", help=tr("cli.help.api_key"))
    parser.add_argument("--context-window", help=tr("cli.help.context_window"))
    parser.add_argument("--lang", help=tr("cli.help.lang"))
    parser.add_argument("--style", help=tr("cli.help.style"))
    parser.add_argument("--mode", help=tr("cli.help.mode"))
    parser.add_argument("--session", help=tr("cli.help.session"))
    parser.add_argument("--new-session", action="store_true", help=tr("cli.help.new_session"))
    parser.add_argument("--skip-connection-test", action="store_true", help=tr("cli.help.skip_connection_test"))
    parser.add_argument("--no-stream", action="store_true", help=tr("cli.help.no_stream"))
    parser.add_argument("--no-clear", action="store_true", help=tr("cli.help.no_clear"))
    parser.add_argument("--doctor", action="store_true", help=tr("cli.help.doctor"))
    parser.add_argument("--json", action="store_true", help=tr("cli.help.json"))
    parser.add_argument("--bundle", help=tr("cli.help.bundle"))
    return parser
