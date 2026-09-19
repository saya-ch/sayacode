"""
权限确认模块，提供仿 Claude Code 的弹窗式确认。

核心函数为 configure_permission_confirmation、build_interrupt_handler 与
build_deny_interrupt_handler，PermissionDialogQueue 负责排队展示，
供交互循环与 headless 流程按场景装配。
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
from lib.cli.theme import (
    console,
    print_error,
    print_info,
    print_success,
    SayacodeColors,
)
from lib.core.permissions import (
    DANGEROUS_TOOLS,
    PermissionRequest,
    _active_runtime,
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
    ("save", "permission.allow_permanent", "cyan"),
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
        "\n\n↑/↓ 切换，Enter 确认；y/a/s/n 可快速选择",
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


# 提供快捷键支持。
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
    if key in ("s", "p", "3"):
        return "save"
    if key in ("n", "4", "\x1b", "esc", "escape"):
        return "deny"
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

    import termios
    import tty

    from lib.cli import ttykeys as _ttykeys

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
            # 复用可注入的共享实现：假 select 可测，真 select 不炸 Windows。
            sequence = _ttykeys.read_escape_tail()
            if sequence == "[A":
                return "up"
            if sequence == "[B":
                return "down"
            return "esc"
        return char
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# 追踪会话级拒绝状态。
_denial_tracker = DenialTracker()


def reset_denial_tracker() -> None:
    """会话启动时重置拒绝追踪。"""
    _denial_tracker.reset()
    _sync_fallback_flag()


def _sync_fallback_flag() -> None:
    """把拒绝追踪器的回退态同步给权限运行时，使回退模式真正生效。

    此处不需要 try/except：_active_runtime() 只做 ContextVar 读取，不会抛异常。
    吞掉异常只会让「回退态同步失败」再次变成静默无效 —— 那正是 A2 缺陷的形态。
    """
    _active_runtime().is_in_fallback = _denial_tracker.is_in_fallback


def _cleanup_confirm() -> None:
    console.control("\033[u")  # 恢复光标位置。
    console.control("\033[J")  # 清空光标以下内容。


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
        if request.tool_name in DANGEROUS_TOOLS:
            # 限制危险工具仅本次放行，不写 session 规则与策略文件。
            # 写入会被降级为 deny，导致后续调用直接拒绝且不再询问。
            print_error(
                tr("common.warning")
                + f": {request.tool_name} 属于危险工具，不支持会话级放行，仅本次生效。"
            )
            return True
        update_session_permission_rules({request.tool_name: "allow"})
        print_success(tr("permission.session_set", tool=request.tool_name))
        return True
    elif selected_choice == "save":
        if request.tool_name in DANGEROUS_TOOLS:
            # 危险工具不允许永久放行：set_tool_permission 会抛 ValueError，
            # 而 project→user 的回退同样会抛，所以必须在这里拦住（否则直接崩溃）。
            print_error(
                tr("common.warning")
                + f": {request.tool_name} 属于危险工具，不支持永久放行，仅本次生效。"
            )
            return True
        try:
            path = set_tool_permission(request.tool_name, "allow", scope="project")
        except ValueError:
            # project scope 需要工作区，回退到 user scope。
            path = set_tool_permission(request.tool_name, "allow", scope="user")
        print_success(tr("permission.permanent_set", tool=request.tool_name))
        print_info(tr("common.saved_to", path=path))
        return True
    elif selected_choice == "deny":
        _denial_tracker.record_denial()
        if _denial_tracker.should_fallback_to_prompting():
            _denial_tracker.enter_fallback_mode()
            _sync_fallback_flag()
            print_error(tr("common.warning") + ": 连续拒绝已达阈值，后续操作将逐项询问。")
        return False

    _denial_tracker.record_success()
    return True


def configure_permission_confirmation(enabled: bool) -> None:
    """注册或移除交互式权限确认回调。"""
    set_permission_confirm_callback(_confirm_tool_permission if enabled else None)


def build_interrupt_handler() -> Callable[[dict], dict]:
    """图中断 → 现有确认窗：把 ``tool_ask`` 载荷翻译成批准答案。

    批准后的落规则副作用（会话/永久）仍由 ``_confirm_tool_permission`` 内部完成，
    与今天一致；"一次"的那份由中间件 ``grant_once()`` 补——否则恢复后工具体内联
    check 会再弹一次窗。未知种类按拒绝（fail-closed）。
    """
    from lib.core.middleware import INTERRUPT_TOOL_ASK

    def _handle(payload: dict) -> dict:
        if not isinstance(payload, dict) or payload.get("kind") != INTERRUPT_TOOL_ASK:
            return {"approved": False}
        request = PermissionRequest(
            tool_name=str(payload.get("tool") or "tool"),
            action="ask",
            arguments_preview=str(payload.get("args_preview") or "{}"),
            source=str(payload.get("source") or "policy"),
        )
        return {"approved": bool(_confirm_tool_permission(request))}

    return _handle


def build_deny_interrupt_handler() -> Callable[[dict], dict]:
    """无人值守：一切询问按拒绝（与 ``configure_permission_confirmation(False)``
    同姿态，显式写出来免得靠默认行为猜）。"""
    return lambda payload: {"approved": False}


# ===========================================================================
# 管理权限弹窗队列。
# ===========================================================================

@dataclass
class PermissionDialog:
    """描述一次待确认的权限弹窗请求。"""
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
        """将弹窗加入等待队列。"""
        self._queue.append(dialog)

    def dequeue(self) -> PermissionDialog | None:
        """取出队首弹窗；队列为空时返回 None。"""
        if self._queue:
            self._current = self._queue.pop(0)
            return self._current
        return None

    @property
    def has_pending(self) -> bool:
        """队列中是否还有等待展示的弹窗。"""
        return len(self._queue) > 0

    @property
    def queue_size(self) -> int:
        """返回等待队列当前长度。"""
        return len(self._queue)
