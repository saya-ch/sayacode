"""会话管理门面，老路径兼容垫片。

实现已拆到三个子模块，压缩最终效果不变。
本文件只做再导出，不放逻辑。
"""

from __future__ import annotations

from .session_messages import (
    Message as Message,
    SessionManager as SessionManager,
    SessionDerivedMemoryView as SessionDerivedMemoryView,
    load_legacy_memory_json as load_legacy_memory_json,
    normalize_truncate_target as normalize_truncate_target,
    _budgets_for_limit as _budgets_for_limit,
)

from .session_compact import (
    _COMPACT_SUMMARY_PROMPT as _COMPACT_SUMMARY_PROMPT,
    _CONTEXT_BUDGET_RATIO as _CONTEXT_BUDGET_RATIO,
    _DEFAULT_CONTEXT_LIMIT as _DEFAULT_CONTEXT_LIMIT,
    _KEEP_FULL_ROUNDS as _KEEP_FULL_ROUNDS,
    _OUTPUT_RESERVE_RATIO as _OUTPUT_RESERVE_RATIO,
    _PREVENTIVE_RATIO as _PREVENTIVE_RATIO,
    _SUMMARIZE_ROUNDS as _SUMMARIZE_ROUNDS,
    _SYSTEM_OVERHEAD_ESTIMATE as _SYSTEM_OVERHEAD_ESTIMATE,
    _URGENT_RATIO as _URGENT_RATIO,
)

from .session_store import (
    SESSION_SCHEMA_VERSION as SESSION_SCHEMA_VERSION,
)

from .private_io import (
    write_private_json as write_private_json,
)

__all__ = [
    "Message",
    "SESSION_SCHEMA_VERSION",
    "SessionDerivedMemoryView",
    "SessionManager",
    "_COMPACT_SUMMARY_PROMPT",
    "_CONTEXT_BUDGET_RATIO",
    "_DEFAULT_CONTEXT_LIMIT",
    "_KEEP_FULL_ROUNDS",
    "_OUTPUT_RESERVE_RATIO",
    "_PREVENTIVE_RATIO",
    "_SUMMARIZE_ROUNDS",
    "_SYSTEM_OVERHEAD_ESTIMATE",
    "_URGENT_RATIO",
    "_budgets_for_limit",
    "load_legacy_memory_json",
    "normalize_truncate_target",
    "write_private_json",
]
