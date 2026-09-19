"""slash command 接口包入口。

集中重导出会话、模型、运行时与工作区等 handler，核心函数为
build_default_command_router，供交互循环按输入分发调用。

包根用惰性导出：import 子模块时不再连带拖入全部 handler，
避免和顶层垫片形成循环导入。
"""

_LAZY = {
    "CommandContext": "lib.commands.base",
    "CommandHandler": "lib.commands.base",
    "CommandRouter": "lib.commands.router",
    "AnalyzeCommandHandler": "lib.commands.runtime_info",
    "ClearCommandHandler": "lib.commands.conversation",
    "ConfigCommandHandler": "lib.commands.model",
    "CompactCommandHandler": "lib.commands.conversation",
    "ContextCommandHandler": "lib.commands.conversation",
    "CustomCommandsCommandHandler": "lib.commands.workspace",
    "DoctorCommandHandler": "lib.commands.diagnostics",
    "GitCommandHandler": "lib.commands.runtime_info",
    "GuideCommandHandler": "lib.commands.conversation",
    "HelpCommandHandler": "lib.commands.conversation",
    "HistoryCommandHandler": "lib.commands.conversation",
    "HooksCommandHandler": "lib.commands.hooks",
    "LanguageCommandHandler": "lib.commands.preferences",
    "McpCommandHandler": "lib.commands.mcp",
    "ModelCommandHandler": "lib.commands.model",
    "ModeCommandHandler": "lib.commands.mode",
    "PathsCommandHandler": "lib.commands.workspace",
    "PermissionsCommandHandler": "lib.commands.permissions",
    "PrefsCommandHandler": "lib.commands.preferences",
    "QuitCommandHandler": "lib.commands.conversation",
    "ResetCommandHandler": "lib.commands.runtime_info",
    "SessionCommandHandler": "lib.commands.session",
    "SettingsCommandHandler": "lib.commands.preferences",
    "StatsCommandHandler": "lib.commands.runtime_info",
    "StatusCommandHandler": "lib.commands.runtime_info",
    "StyleCommandHandler": "lib.commands.preferences",
    "SymbolsCommandHandler": "lib.commands.symbols",
    "ToolsCommandHandler": "lib.commands.tools",
    "WorkspaceCommandHandler": "lib.commands.workspace",
    "build_default_command_router": "lib.commands.runtime_handlers",
    "normalize_command_name": "lib.commands.router",
    "parse_command": "lib.commands.router",
}


def __getattr__(name: str):
    """首次访问时才解析对应子模块。"""
    if name in _LAZY:
        import importlib

        module = importlib.import_module(_LAZY[name])
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
