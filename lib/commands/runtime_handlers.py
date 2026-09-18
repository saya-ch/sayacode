"""当前 CLI 命令面的默认运行时 command router。

组装全部公开 slash command handler，核心函数为
build_default_command_router，供交互循环初始化时调用。
"""

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
from .plan import PlanCommandHandler
from .rewind import RewindCommandHandler
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
from .team import TeamCommandHandler
from .trace import TraceCommandHandler
from .tools import ToolsCommandHandler
from .workspace import (
    CustomCommandsCommandHandler,
    PathsCommandHandler,
    WorkspaceCommandHandler,
)


def build_default_command_router() -> CommandRouter:
    """构建默认的公开 slash command router。"""
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
        TeamCommandHandler(),
        RewindCommandHandler(),
        TraceCommandHandler(),
        PlanCommandHandler(),
    ]
    return CommandRouter(handlers)


__all__ = ["build_default_command_router"]
