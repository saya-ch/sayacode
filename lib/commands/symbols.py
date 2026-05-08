"""Symbols slash command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ..core.symbols import SymbolIndex, render_symbols
from ..i18n import tr
from ..runtime import RuntimeContext
from ..theme import console, print_status
from .base import CommandContext, CommandHandler


@dataclass
class SymbolsCommandHandler(CommandHandler):
    """Show the active workspace's static symbol index."""

    name: str = "symbols"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        query = command.args.strip()
        state = runtime.app_state
        workspace = getattr(state, "workspace", runtime.workspace)

        print_status(tr("symbols.indexing"))
        index = SymbolIndex(workspace)
        symbols = index.scan()
        if query:
            symbols = index.search(query=query, limit=80)
            console.print(render_symbols(symbols, title=tr("symbols.matching", query=query)))
        else:
            counts: Dict[str, int] = {}
            for symbol in symbols:
                counts[symbol.kind] = counts.get(symbol.kind, 0) + 1
            lines = [
                tr("help.symbols"),
                f"{tr('workspace.path')}: {workspace}",
                f"{tr('common.total', default='Total')}: {len(symbols)}",
            ]
            for kind, count in sorted(counts.items()):
                lines.append(f"{kind}: {count}")
            console.print("\n".join(lines))
            console.print()
            console.print(render_symbols(index.search(limit=80), title=tr("symbols.top")))
        console.print()
        return True


__all__ = ["SymbolsCommandHandler"]
