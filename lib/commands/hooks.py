"""Hooks slash command."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.hooks import (
    get_hook_audit_log,
    render_hook_status,
    trust_hook_workspace,
    untrust_hook_workspace,
)
from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import console, print_error, print_info, print_success, print_summary_card
from .base import CommandContext, CommandHandler


@dataclass
class HooksCommandHandler(CommandHandler):
    """Inspect hook status, audit log, and project hook trust."""

    name: str = "hooks"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        parts = command.raw.strip().split()
        action = parts[1].lower() if len(parts) > 1 else "status"

        if action in {"status", "list", "show"}:
            console.print()
            console.print(render_hook_status())
            console.print()
            print_summary_card(
                tr("hooks.title"),
                {
                    "/hooks": tr("hooks.status_show"),
                    "/hooks audit": tr("hooks.audit_desc"),
                    "/hooks trust": tr("hooks.trust_desc"),
                    "/hooks untrust": tr("hooks.untrust_desc"),
                },
                footer=tr("hooks.usage"),
            )
            return True

        if action == "audit":
            rows = {}
            for index, entry in enumerate(get_hook_audit_log()[-10:], 1):
                rows[str(index)] = (
                    f"{entry.get('event')} {entry.get('name')} "
                    f"rc={entry.get('returncode')} blocked={entry.get('blocked')}"
                )
            print_summary_card(tr("permissions.audit_title"), rows or {tr("common.empty"): tr("permissions.no_audit")})
            return True

        if action == "trust":
            path = trust_hook_workspace(runtime.workspace)
            print_success(tr("hooks.trusted"))
            print_info(tr("common.saved_to", path=path))
            return True

        if action == "untrust":
            path = untrust_hook_workspace(runtime.workspace)
            print_success(tr("hooks.untrusted"))
            print_info(tr("common.saved_to", path=path))
            return True

        print_error(tr("hooks.usage"))
        return True


__all__ = ["HooksCommandHandler"]
