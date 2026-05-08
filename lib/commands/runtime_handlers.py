"""Default runtime command router for the current CLI command surface."""

from __future__ import annotations

from .conversation import (
    ClearCommandHandler,
    CompactCommandHandler,
    ContextCommandHandler,
    GuideCommandHandler,
    HelpCommandHandler,
    HistoryCommandHandler,
    QuitCommandHandler,
)
from .diagnostics import DoctorCommandHandler
from .hooks import HooksCommandHandler
from .mcp import McpCommandHandler
from .model import ConfigCommandHandler, ModelCommandHandler
from .mode import ModeCommandHandler
from .permissions import PermissionsCommandHandler
from .preferences import (
    LanguageCommandHandler,
    PrefsCommandHandler,
    SettingsCommandHandler,
    StyleCommandHandler,
)
from .router import CommandRouter
from .runtime_info import (
    AnalyzeCommandHandler,
    GitCommandHandler,
    ResetCommandHandler,
    StatsCommandHandler,
    StatusCommandHandler,
)
from .session import SessionCommandHandler
from .symbols import SymbolsCommandHandler
from .tools import ToolsCommandHandler
from .workspace import (
    CustomCommandsCommandHandler,
    PathsCommandHandler,
    WorkspaceCommandHandler,
)


def build_default_command_router() -> CommandRouter:
    """Build the default public slash-command router."""
    handlers = [
        HelpCommandHandler(),
        GuideCommandHandler(),
        PrefsCommandHandler(),
        LanguageCommandHandler(),
        StyleCommandHandler(),
        ModeCommandHandler(),
        ClearCommandHandler(),
        CompactCommandHandler(),
        HistoryCommandHandler(),
        SessionCommandHandler(),
        ContextCommandHandler(),
        SymbolsCommandHandler(),
        StatusCommandHandler(),
        WorkspaceCommandHandler(),
        PathsCommandHandler(),
        CustomCommandsCommandHandler(),
        PermissionsCommandHandler(),
        DoctorCommandHandler(),
        HooksCommandHandler(),
        ModelCommandHandler(),
        SettingsCommandHandler(),
        ConfigCommandHandler(),
        McpCommandHandler(),
        QuitCommandHandler(),
        ToolsCommandHandler(),
        StatsCommandHandler(),
        AnalyzeCommandHandler(),
        ResetCommandHandler(),
        GitCommandHandler(),
    ]
    return CommandRouter(handlers)


__all__ = ["build_default_command_router"]
