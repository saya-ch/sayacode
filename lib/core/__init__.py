"""
核心模块

包含会话管理、项目上下文、记忆系统和安全检查等功能。
"""

from .session import SESSION_SCHEMA_VERSION, SessionManager, Message
from .agent_runtime import AgentRunner, ConversationManager, PromptBuilder, TurnTransition, TurnState
from .agent_mailbox import AgentMailbox, MailboxMessage
from .audit import AuditEvent, AuditLogService, append_audit_event, read_recent_audit_events, redact_value
from .context import ProjectContext, FileInfo, ChangeRecord
from .denial_tracker import DenialTracker
from .team_config import TeamConfig, TeamMember
from .team_manager import TeamManager
from .tool_meta import ToolMeta, register_tool_meta, get_deferred_tool_metas, get_searchable_tool_metas
from .worker_manager import WorkerManager, WorkerState, WorkerStatus
from .context_packager import ContextPackage, ContextPackager, ContextPackRequest, TokenEstimate, TokenEstimator
from .memory import MemoryManager, Interaction, FileModification
from .permissions import (
    DANGEROUS_TOOLS,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRuleSet,
    SOURCE_BUILTIN,
    SOURCE_PROJECT,
    SOURCE_SESSION,
    SOURCE_USER,
    configure_permission_workspace,
    enforce_tool_permission,
    get_permission_policy_summary,
    set_permission_confirm_callback,
    set_session_permission_rules,
    set_tool_permission,
)
from .doctor import (
    DiagnosticCheck,
    build_support_bundle,
    has_failed_checks,
    render_doctor_report,
    run_doctor_checks,
    write_support_bundle,
)
from .hooks import (
    configure_hooks_workspace,
    get_hook_audit_log,
    get_hook_status,
    render_hook_status,
    trigger_hook_event,
    trust_hook_workspace,
    untrust_hook_workspace,
)
from .modes import (
    AgentMode,
    agent_mode_label,
    apply_agent_mode_permissions,
    get_agent_mode,
    get_agent_mode_prompt_overlay,
    list_agent_modes,
    normalize_agent_mode,
    render_agent_mode_summary,
)
from .mcp_runtime import (
    call_mcp_tool,
    configure_mcp_workspace,
    get_mcp_status,
    is_mcp_workspace_trusted,
    load_mcp_tools,
    reload_mcp_tools,
    shutdown_mcp_runtime,
    trust_mcp_workspace,
    untrust_mcp_workspace,
)
from .symbols import CodeSymbol, SymbolIndex, index_project_symbols, render_symbols, summarize_symbol_index
from .private_io import ensure_private_dir, restrict_permissions, write_private_json, write_private_text
from .paths import ConfigStore, SayacodePaths, StateStore
from .safety import SafetyChecker, SafetyLevel, SafetyResult, Operation

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
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionRuleSet",
    "SOURCE_BUILTIN",
    "SOURCE_PROJECT",
    "SOURCE_SESSION",
    "SOURCE_USER",
    "configure_permission_workspace",
    "enforce_tool_permission",
    "get_permission_policy_summary",
    "set_permission_confirm_callback",
    "set_session_permission_rules",
    "set_tool_permission",

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

    # Agent 模式
    "AgentMode",
    "agent_mode_label",
    "apply_agent_mode_permissions",
    "get_agent_mode",
    "get_agent_mode_prompt_overlay",
    "list_agent_modes",
    "normalize_agent_mode",
    "render_agent_mode_summary",

    # MCP runtime
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

    # Agent 运行时组件
    "TurnTransition",
    "TurnState",

    # 多 Agent 协作
    "AgentMailbox",
    "MailboxMessage",
    "TeamConfig",
    "TeamMember",
    "TeamManager",
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
