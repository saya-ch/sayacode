"""
核心模块

包含会话管理、项目上下文、记忆系统和安全检查等功能。

本包 ``__init__`` 刻意保持轻量：全部惰性加载（PEP 562）。之前这里 eager
import 全家桶，还引发过真实的循环导入：``lib.core.doctor`` 回指
``lib.api_config``，旧导入顺序下靠运气没爆，切了顶层惰性后立刻现形。
"""

_LAZY_EXPORTS = {
    # 会话管理
    "SESSION_SCHEMA_VERSION": ".session",
    "SessionManager": ".session",
    "Message": ".session",
    "AgentRunner": ".agent_runtime",
    "ConversationManager": ".agent_runtime",
    "PromptBuilder": ".agent_runtime",
    "TurnTransition": ".agent_runtime",
    "TurnState": ".agent_runtime",
    # 多 Agent 协作
    "AgentMailbox": ".agent_mailbox",
    "MailboxMessage": ".agent_mailbox",
    "TeamConfig": ".team_config",
    "TeamMember": ".team_config",
    "TeamManager": ".team_manager",
    "TeamWorktree": ".team_worktree",
    "TeamWorktreeManager": ".team_worktree",
    "WorktreeIsolationError": ".team_worktree",
    "WorkerManager": ".worker_manager",
    "WorkerState": ".worker_manager",
    "WorkerStatus": ".worker_manager",
    # 审计
    "AuditEvent": ".audit",
    "AuditLogService": ".audit",
    "append_audit_event": ".audit",
    "read_recent_audit_events": ".audit",
    "redact_value": ".audit",
    # 项目上下文
    "ProjectContext": ".context",
    "FileInfo": ".context",
    "ChangeRecord": ".context",
    "ContextPackage": ".context_packager",
    "ContextPackager": ".context_packager",
    "ContextPackRequest": ".context_packager",
    "TokenEstimate": ".context_packager",
    "TokenEstimator": ".context_packager",
    # 记忆系统
    "MemoryManager": ".memory",
    "Interaction": ".memory",
    "FileModification": ".memory",
    # 拒绝追踪
    "DenialTracker": ".denial_tracker",
    # 工具元数据
    "ToolMeta": ".tool_meta",
    "register_tool_meta": ".tool_meta",
    "get_deferred_tool_metas": ".tool_meta",
    "get_searchable_tool_metas": ".tool_meta",
    # 权限策略
    "DANGEROUS_TOOLS": ".permissions",
    "DEFAULT_COMMAND_RULES": ".permissions",
    "PermissionDecision": ".permissions",
    "PermissionPolicy": ".permissions",
    "PermissionRequest": ".permissions",
    "SessionPermissionState": ".permissions",
    "SOURCE_BUILTIN": ".permissions",
    "SOURCE_PROJECT": ".permissions",
    "SOURCE_SESSION": ".permissions",
    "SOURCE_USER": ".permissions",
    "clear_mode_permission_rules": ".permissions",
    "configure_permission_workspace": ".permissions",
    "enforce_tool_permission": ".permissions",
    "get_permission_policy_summary": ".permissions",
    "set_mode_permission_rules": ".permissions",
    "set_permission_confirm_callback": ".permissions",
    "set_session_permission_rules": ".permissions",
    "set_tool_permission": ".permissions",
    "update_session_permission_rules": ".permissions",
    # 自诊断
    "DiagnosticCheck": ".doctor",
    "build_support_bundle": ".doctor",
    "has_failed_checks": ".doctor",
    "render_doctor_report": ".doctor",
    "run_doctor_checks": ".doctor",
    "write_support_bundle": ".doctor",
    # 生命周期 hooks
    "configure_hooks_workspace": ".hooks",
    "get_hook_audit_log": ".hooks",
    "get_hook_status": ".hooks",
    "render_hook_status": ".hooks",
    "trigger_hook_event": ".hooks",
    "trust_hook_workspace": ".hooks",
    "untrust_hook_workspace": ".hooks",
    # 管理 Agent 模式导出。
    "AgentMode": ".modes",
    "agent_mode_label": ".modes",
    "apply_agent_mode_permissions": ".modes",
    "get_agent_mode": ".modes",
    "get_agent_mode_prompt_overlay": ".modes",
    "list_agent_modes": ".modes",
    "normalize_agent_mode": ".modes",
    "render_agent_mode_summary": ".modes",
    # 管理 MCP 运行时导出。
    "call_mcp_tool": ".mcp_runtime",
    "configure_mcp_workspace": ".mcp_runtime",
    "get_mcp_status": ".mcp_runtime",
    "is_mcp_workspace_trusted": ".mcp_runtime",
    "load_mcp_tools": ".mcp_runtime",
    "reload_mcp_tools": ".mcp_runtime",
    "shutdown_mcp_runtime": ".mcp_runtime",
    "trust_mcp_workspace": ".mcp_runtime",
    "untrust_mcp_workspace": ".mcp_runtime",
    # 静态符号索引
    "CodeSymbol": ".symbols",
    "SymbolIndex": ".symbols",
    "index_project_symbols": ".symbols",
    "render_symbols": ".symbols",
    "summarize_symbol_index": ".symbols",
    # 私有本地状态写入
    "ensure_private_dir": ".private_io",
    "restrict_permissions": ".private_io",
    "write_private_json": ".private_io",
    "write_private_text": ".private_io",
    "ConfigStore": ".paths",
    "SayacodePaths": ".paths",
    "StateStore": ".paths",
    # 安全检查
    "SafetyChecker": ".safety",
    "SafetyLevel": ".safety",
    "SafetyResult": ".safety",
    "Operation": ".safety",
}


def __getattr__(name: str):
    """PEP 562 惰性导出：首次访问时才 import 对应子模块。"""
    if name in _LAZY_EXPORTS:
        import importlib

        module = importlib.import_module(_LAZY_EXPORTS[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # 会话管理
    "SessionManager",
    "Message",
    "SESSION_SCHEMA_VERSION",
    "AgentRunner",
    "ConversationManager",
    "PromptBuilder",
    "AuditEvent",
    "AuditLogService",
    "append_audit_event",
    "read_recent_audit_events",
    "redact_value",

    # 项目上下文
    "ProjectContext",
    "FileInfo",
    "ChangeRecord",
    "ContextPackage",
    "ContextPackager",
    "ContextPackRequest",
    "TokenEstimate",
    "TokenEstimator",

    # 记忆系统
    "MemoryManager",
    "Interaction",
    "FileModification",

    # 权限策略
    "DANGEROUS_TOOLS",
    "DEFAULT_COMMAND_RULES",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRequest",
    "SessionPermissionState",
    "SOURCE_BUILTIN",
    "SOURCE_PROJECT",
    "SOURCE_SESSION",
    "SOURCE_USER",
    "clear_mode_permission_rules",
    "configure_permission_workspace",
    "enforce_tool_permission",
    "get_permission_policy_summary",
    "set_mode_permission_rules",
    "set_permission_confirm_callback",
    "set_session_permission_rules",
    "set_tool_permission",
    "update_session_permission_rules",

    # 自诊断
    "DiagnosticCheck",
    "build_support_bundle",
    "has_failed_checks",
    "render_doctor_report",
    "run_doctor_checks",
    "write_support_bundle",

    # 生命周期 hooks
    "configure_hooks_workspace",
    "get_hook_audit_log",
    "get_hook_status",
    "render_hook_status",
    "trigger_hook_event",
    "trust_hook_workspace",
    "untrust_hook_workspace",

    # 管理 Agent 模式导出。
    "AgentMode",
    "agent_mode_label",
    "apply_agent_mode_permissions",
    "get_agent_mode",
    "get_agent_mode_prompt_overlay",
    "list_agent_modes",
    "normalize_agent_mode",
    "render_agent_mode_summary",

    # 管理 MCP 运行时导出。
    "call_mcp_tool",
    "configure_mcp_workspace",
    "get_mcp_status",
    "is_mcp_workspace_trusted",
    "load_mcp_tools",
    "reload_mcp_tools",
    "shutdown_mcp_runtime",
    "trust_mcp_workspace",
    "untrust_mcp_workspace",

    # 静态符号索引
    "CodeSymbol",
    "SymbolIndex",
    "index_project_symbols",
    "render_symbols",
    "summarize_symbol_index",

    # 私有本地状态写入
    "ensure_private_dir",
    "restrict_permissions",
    "write_private_json",
    "write_private_text",
    "ConfigStore",
    "SayacodePaths",
    "StateStore",

    # 安全检查
    "SafetyChecker",
    "SafetyLevel",
    "SafetyResult",
    "Operation",

    # 管理 Agent 运行时组件。
    "TurnTransition",
    "TurnState",

    # 多 Agent 协作
    "AgentMailbox",
    "MailboxMessage",
    "TeamConfig",
    "TeamMember",
    "TeamManager",
    "TeamWorktree",
    "TeamWorktreeManager",
    "WorktreeIsolationError",
    "WorkerManager",
    "WorkerState",
    "WorkerStatus",

    # 工具元数据
    "ToolMeta",
    "register_tool_meta",
    "get_deferred_tool_metas",
    "get_searchable_tool_metas",

    # 拒绝追踪
    "DenialTracker",
]
