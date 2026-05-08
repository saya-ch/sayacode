"""Tools slash command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import SayacodeColors, console, print_banner
from ..tools import get_runtime_tool_catalog
from .base import CommandContext, CommandHandler


_TOOL_GROUP_LABELS = {
    "lib.tools.file_tools": "tools.group.file",
    "lib.tools.shell_tools": "tools.group.shell",
    "lib.tools.git_tools": "tools.group.git",
    "lib.tools.project_tools": "tools.group.project",
}


@dataclass
class ToolsCommandHandler(CommandHandler):
    """Show the actual runtime tool catalog."""

    name: str = "tools"
    aliases: tuple[str, ...] = ("tool",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        catalog = _runtime_tool_catalog(runtime)
        total_tools = sum(len(items) for items in catalog.values())

        print_banner(tr("tools.title"), tr("tools.runtime_subtitle", count=total_tools))
        for group, items in catalog.items():
            console.print(f"[{SayacodeColors.SECONDARY}]{group} ({len(items)})[/]")
            for item in items:
                summary = item.get("summary") or "-"
                console.print(f"  - [{SayacodeColors.TEXT_BRIGHT}]{item['name']}[/]: {summary}")
            console.print()

        console.print(f"[{SayacodeColors.TEXT_DIM}]{tr('tools.generated_footer')}[/]")
        console.print()
        return True


def _runtime_tool_catalog(runtime: RuntimeContext) -> Dict[str, List[dict]]:
    tools = list(runtime.tools or getattr(runtime.agent, "tools", []) or [])
    if not tools:
        return get_runtime_tool_catalog()

    catalog: Dict[str, List[dict]] = {}
    for tool in tools:
        item = _tool_catalog_item(tool)
        catalog.setdefault(item["group"], []).append(item)

    for items in catalog.values():
        items.sort(key=lambda item: item["name"])
    return dict(sorted(catalog.items(), key=lambda pair: pair[0]))


def _tool_catalog_item(tool: Any) -> dict:
    source_module = getattr(getattr(tool, "func", None), "__module__", "") or getattr(tool, "__module__", "")
    group = tr(_TOOL_GROUP_LABELS.get(source_module, "tools.group.other"))
    name = str(getattr(tool, "name", tool.__class__.__name__))
    if name.startswith("mcp_"):
        group = tr("tools.group.mcp")
    description = getattr(tool, "description", "") or ""
    summary = description.strip().splitlines()[0] if description.strip() else ""
    return {
        "name": name,
        "summary": summary,
        "module": source_module,
        "group": group,
    }


__all__ = ["ToolsCommandHandler"]
