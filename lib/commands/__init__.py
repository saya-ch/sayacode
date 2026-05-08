"""Slash command interfaces."""

from .base import CommandContext, CommandHandler
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
from .router import CommandRouter, normalize_command_name, parse_command
from .runtime_info import (
    AnalyzeCommandHandler,
    GitCommandHandler,
    ResetCommandHandler,
    StatsCommandHandler,
    StatusCommandHandler,
)
from .runtime_handlers import build_default_command_router
from .session import SessionCommandHandler
from .symbols import SymbolsCommandHandler
from .tools import ToolsCommandHandler
from .workspace import (
    CustomCommandsCommandHandler,
    PathsCommandHandler,
    WorkspaceCommandHandler,
)

__all__ = [
    "CommandContext",
    "CommandHandler",
    "CommandRouter",
    "AnalyzeCommandHandler",
    "ClearCommandHandler",
    "ConfigCommandHandler",
    "CompactCommandHandler",
    "ContextCommandHandler",
    "CustomCommandsCommandHandler",
    "DoctorCommandHandler",
    "GitCommandHandler",
    "GuideCommandHandler",
    "HelpCommandHandler",
    "HistoryCommandHandler",
    "HooksCommandHandler",
    "LanguageCommandHandler",
    "McpCommandHandler",
    "ModelCommandHandler",
    "ModeCommandHandler",
    "PathsCommandHandler",
    "PermissionsCommandHandler",
    "PrefsCommandHandler",
    "QuitCommandHandler",
    "ResetCommandHandler",
    "SessionCommandHandler",
    "SettingsCommandHandler",
    "StatsCommandHandler",
    "StatusCommandHandler",
    "StyleCommandHandler",
    "SymbolsCommandHandler",
    "ToolsCommandHandler",
    "WorkspaceCommandHandler",
    "build_default_command_router",
    "normalize_command_name",
    "parse_command",
]
