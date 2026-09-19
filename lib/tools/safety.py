"""安全检查工具（兼容 re-export：实现已下沉到 lib.core.safety_rules）。

历史调用方继续从这里导入，行为不变。
"""

from ..core.safety_rules import (
    DANGEROUS_COMMAND_PATTERNS,
    DANGEROUS_EXTENSIONS,
    DANGEROUS_PATH_PATTERNS,
    SafetyResult,
    check_batch_operation,
    check_command_danger,
    check_delete_danger,
    check_file_danger,
    check_sensitive_file,
    check_write_operation,
    filter_dangerous_chars,
    find_safety_target,
    get_danger_level,
    sanitize_path,
)

__all__ = [
    'SafetyResult',
    'check_file_danger',
    'check_delete_danger',
    'check_command_danger',
    'check_batch_operation',
    'find_safety_target',
    'get_danger_level',
    'sanitize_path',
    'check_write_operation',
    'check_sensitive_file',
    'filter_dangerous_chars',
    'DANGEROUS_COMMAND_PATTERNS',
    'DANGEROUS_PATH_PATTERNS',
    'DANGEROUS_EXTENSIONS',
]
