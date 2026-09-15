"""Permissions slash 命令。"""

from __future__ import annotations

from dataclasses import dataclass
import json

from ..core.audit import read_recent_audit_events
from ..core.permissions import (
    _active_runtime,
    get_permission_audit_log,
    get_permission_policy_summary,
    set_tool_permission,
)
from ..i18n import tr
from ..theme import console, print_error, print_info, print_success, print_summary_card
from ..runtime import RuntimeContext
from .base import CommandContext, CommandHandler


@dataclass
class PermissionsCommandHandler(CommandHandler):
    """查看或更新工具权限规则。"""

    name: str = "permissions"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        parts = command.raw.strip().split()
        if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() in {"list", "show"}):
            console.print()
            console.print(get_permission_policy_summary())
            console.print()
            print_summary_card(
                tr("permissions.title"),
                {
                    "/permissions allow <tool> [user|project]": tr("permissions.allow_desc"),
                    "/permissions ask <tool> [user|project]": tr("permissions.ask_desc"),
                    "/permissions deny <tool> [user|project]": tr("permissions.deny_desc"),
                    "/permissions reset": tr("permissions.reset_desc"),
                    "/permissions audit": tr("permissions.audit_desc"),
                },
                footer=tr("permissions.usage"),
            )
            return True

        action = parts[1].lower()
        if action == "audit":
            audit_entries = get_permission_audit_log()[-50:]
            if not audit_entries:
                audit_entries = [
                    entry for entry in read_recent_audit_events(limit=50)
                    if entry.get("type") in {"permission", "permission_policy"}
                ]
            if "--json" in parts[2:]:
                console.print(json.dumps(audit_entries, ensure_ascii=False, indent=2))
                return True
            rows = {}
            for index, entry in enumerate(audit_entries[-10:], 1):
                rows[str(index)] = (
                    f"{entry.get('tool', entry.get('action'))} -> {entry.get('action')} "
                    f"allowed={entry.get('allowed')} source={entry.get('source')}"
                )
            fallback = {tr("common.empty"): tr("permissions.no_audit")}
            print_summary_card(tr("permissions.audit_title"), rows or fallback)
            return True

        # 会话授权此前无法撤销：一次误点的「会话始终允许」会一直生效到进程结束。
        # reset/clear 只清 session 授权，保留 mode 规则（否则 plan 模式的只读约束会被顺手清掉）。
        if action in {"reset", "clear"}:
            # context 可能还没绑定 runtime.permissions；所有 runtime 共享同一份
            # 会话状态（SessionPermissionState），退回进程级运行时是等价的。
            target = runtime.permissions if runtime.permissions is not None else _active_runtime()
            target.clear_session_rules()
            print_success(tr("permissions.reset_done"))
            return True

        if action not in {"allow", "ask", "deny"}:
            print_error(tr("permissions.usage"))
            return True

        if len(parts) < 3:
            print_error(tr("permissions.usage"))
            return True

        tool_name = parts[2]
        scope = parts[3].lower() if len(parts) > 3 else "user"
        try:
            path = set_tool_permission(tool_name, action, scope=scope)
        except ValueError as exc:
            print_error(str(exc))
            return True

        print_success(tr("permissions.updated", tool=tool_name, action=action))
        print_info(tr("common.saved_to", path=path))
        return True


__all__ = ["PermissionsCommandHandler"]
