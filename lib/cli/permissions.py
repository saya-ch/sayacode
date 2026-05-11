"""
权限确认模块 — 仿 Claude Code 的弹窗式确认
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich import box

from lib.core.denial_tracker import DenialTracker
from lib.theme import (
    console,
    print_error,
    print_info,
    print_success,
    SayacodeColors,
)
from lib.core.permissions import (
    PermissionRequest,
    set_permission_confirm_callback,
    set_tool_permission,
    update_session_permission_rules,
)
from lib.i18n import tr


def _format_permission_args(tool_name: str, preview_json: str) -> str:
    try:
        args = json.loads(preview_json) if preview_json.startswith("{") else {}
    except json.JSONDecodeError:
        args = {}

    if not args:
        return preview_json[:120]

    if tool_name == "write_file":
        return tr("permission.write_file", path=args.get("path", "?"), chars=args.get("content_length", "?"))
    elif tool_name == "search_replace":
        return tr("permission.search_replace", path=args.get("path", "?"))
    elif tool_name == "delete_file":
        return tr("permission.delete_file", path=args.get("path", "?"))
    elif tool_name == "create_directory":
        return tr("permission.create_directory", path=args.get("path", "?"))
    elif tool_name == "execute_command_tool":
        return tr("permission.execute_command", command=args.get("command", "?")[:300])
    elif tool_name in ("git_add", "git_commit", "git_checkout", "git_stash", "git_pull", "git_push"):
        return json.dumps(args, ensure_ascii=False, sort_keys=True)[:200]
    elif tool_name == "read_output_file":
        return tr("permission.read_output_file", path=args.get("path", "?"))

    return json.dumps(args, ensure_ascii=False, sort_keys=True)[:200]


_CONFIRM_CHOICES = (
    ("once", "permission.allow_once", "green"),
    ("session", "permission.allow_session", "yellow"),
    ("deny", "permission.deny", "red"),
)


def _build_confirm_panel(tool_name: str, context: str, selected_index: int = 0) -> Panel:
    body = Text()
    body.append(Text(context, style=SayacodeColors.TEXT_DIM))
    body.append("\n\n")
    for index, (_, label_key, color) in enumerate(_CONFIRM_CHOICES):
        if index:
            body.append("\n")
        selected = index == selected_index
        prefix = "› " if selected else "  "
        style = f"bold {color}" if selected else color
        body.append(Text(prefix + tr(label_key), style=style))
    footer = Text(
        "\n\n↑/↓ 切换，Enter 确认；y/a/n 可快速选择",
        style=SayacodeColors.TEXT_DIM,
    )
    body.append(footer)
    return Panel(
        body,
        title=Text(f"  {tool_name}  ", style=f"bold {SayacodeColors.PRIMARY}"),
        border_style=SayacodeColors.BORDER_BRIGHT,
        box=box.ROUNDED,
        padding=(1, 2),
    )


# ── Hotkey support ──────────────────────────────────────────────────────────
def _supports_interactive_input() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def _safe_console_input(prompt: str, default: str = "") -> str:
    try:
        return console.input(prompt)
    except EOFError:
        return default


def _safe_secret_input(prompt: str, default: str = "") -> str:
    try:
        return console.input(prompt, password=True)
    except (TypeError, EOFError):
        try:
            import getpass
            return getpass.getpass(prompt)
        except EOFError:
            return default


def _choice_from_key(key: str) -> Optional[str]:
    key = key.strip().lower()
    if key in ("y", "1"):
        return "once"
    if key in ("a", "2"):
        return "session"
    if key in ("n", "3", "\x1b", "esc", "escape"):
        return "deny"
    if key in ("p",):
        return "save"
    return None


def _read_choice_key() -> str:
    if sys.platform.startswith("win"):
        import msvcrt

        char = msvcrt.getwch()
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\x00", "\xe0"):
            second = msvcrt.getwch()
            if second == "H":
                return "up"
            if second == "P":
                return "down"
            return ""
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x1b":
            return "esc"
        return char

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = sys.stdin.read(1)
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x1b":
            sequence = ""
            while select.select([sys.stdin], [], [], 0.01)[0]:
                sequence += sys.stdin.read(1)
                if len(sequence) >= 2:
                    break
            if sequence == "[A":
                return "up"
            if sequence == "[B":
                return "down"
            return "esc"
        return char
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# 会话级拒绝追踪
_denial_tracker = DenialTracker()


def reset_denial_tracker() -> None:
    """会话启动时重置拒绝追踪。"""
    _denial_tracker.reset()


def _cleanup_confirm() -> None:
    console.control("\033[u")  # 恢复光标
    console.control("\033[J")  # 清空光标以下


def _confirm_tool_permission(request: PermissionRequest) -> bool:
    """弹窗式权限确认，不干扰流式输出。"""
    if not _supports_interactive_input():
        return False

    args_context = _format_permission_args(request.tool_name, request.arguments_preview)
    selected_index = 0
    selected_choice = "once"

    try:
        console.print()
        with Live(
            Group(_build_confirm_panel(request.tool_name, args_context, selected_index)),
            console=console,
            refresh_per_second=20,
            transient=True,
        ) as live:
            while True:
                raw = _read_choice_key()
                shortcut_choice = _choice_from_key(raw)
                if shortcut_choice:
                    selected_choice = shortcut_choice
                    break
                if raw == "enter":
                    selected_choice = _CONFIRM_CHOICES[selected_index][0]
                    break
                if raw == "up":
                    selected_index = (selected_index - 1) % len(_CONFIRM_CHOICES)
                elif raw == "down":
                    selected_index = (selected_index + 1) % len(_CONFIRM_CHOICES)
                else:
                    continue
                live.update(Group(_build_confirm_panel(request.tool_name, args_context, selected_index)))
    except (EOFError, KeyboardInterrupt):
        selected_choice = "deny"

    if selected_choice == "session":
        update_session_permission_rules({request.tool_name: "allow"})
        print_success(tr("permission.session_set", tool=request.tool_name))
        return True
    elif selected_choice == "save":
        try:
            path = set_tool_permission(request.tool_name, "allow", scope="project")
        except ValueError:
            path = set_tool_permission(request.tool_name, "allow", scope="user")
        print_success(tr("permission.permanent_set", tool=request.tool_name))
        print_info(tr("common.saved_to", path=path))
        return True
    elif selected_choice == "deny":
        _denial_tracker.record_denial()
        if _denial_tracker.should_fallback_to_prompting():
            _denial_tracker.enter_fallback_mode()
            print_error(tr("common.warning") + ": 连续拒绝已达阈值，后续操作将逐项询问。")
        return False

    _denial_tracker.record_success()
    return True


def configure_permission_confirmation(enabled: bool) -> None:
    """Register or remove the interactive permission confirmation callback."""
    set_permission_confirm_callback(_confirm_tool_permission if enabled else None)


# ═══════════════════════════════════════════════════════════════════════════════
# 权限弹窗队列
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PermissionDialog:
    tool_name: str
    description: str
    risk_level: str = "medium"
    on_allow: Callable[[], None] | None = None
    on_deny: Callable[[], None] | None = None


class PermissionDialogQueue:
    """一次只显示一个权限弹窗，其余的排队。"""
    def __init__(self):
        self._queue: list[PermissionDialog] = []
        self._current: PermissionDialog | None = None

    def enqueue(self, dialog: PermissionDialog) -> None:
        self._queue.append(dialog)

    def dequeue(self) -> PermissionDialog | None:
        if self._queue:
            self._current = self._queue.pop(0)
            return self._current
        return None

    @property
    def has_pending(self) -> bool:
        return len(self._queue) > 0

    @property
    def queue_size(self) -> int:
        return len(self._queue)
