"""
安全检查工具

提供文件操作、命令执行的危险检测功能，防止意外或恶意的危险操作。

危险操作包括：
- 删除系统文件或递归删除目录
- 格式化操作
- 修改系统目录
- 执行未知来源的可执行文件
"""

import re
from pathlib import Path
from typing import Tuple, List
from dataclasses import dataclass


# ==============================================================================
# 危险模式定义
# ==============================================================================

# 命令危险模式
DANGEROUS_COMMAND_PATTERNS = [
    # 递归强制删除
    r'rm\s+-rf\s+',
    r'rm\s+-\s*r\s+-\s*f',
    r'rm\s+-r\s+-f\b',
    r'rm\s+-f\s+-r\b',
    r'del\s+/s\s+/q',
    r'del\s+\/s\s+\/q',
    r'(?:rmdir|rd)\s+/s\s+/q',
    r'rm\s+-rf\b',
    r'remove-item\b.*-recurse\b',
    r'remove-item\b.*-force\b.*-recurse\b',
    r'remove-item\b.*-recurse\b.*-force\b',
    
    # 格式化命令
    r'format\s+',
    r'format\b',
    
    # 危险的网络操作
    r'curl\s+.*\|\s*sh',
    r'wget\s+.*\|\s*sh',
    r'sh\s+<.*http',
    
    # 系统修改
    r'sudo\s+.*\s+rm\s+',
    r'sudo\s+.*\s+del\s+',
    r'\.\./\.\./',  # 目录遍历
    
    # 危险的文件操作
    r'\|\s*sh\b',
    r'exec\s+',
]

# 危险路径模式
DANGEROUS_PATH_PATTERNS = [
    r'^[a-z]:/windows(?:/|$)',
    r'^[a-z]:/program files(?: \(x86\))?(?:/|$)',
    r'^[a-z]:/system(?:/|$)',
    r'^/(?:etc|bin|sbin|usr/bin|usr/sbin|root)(?:/|$)',
    r'^~(?:/|$)',
    r'(?:^|/)\.\.(?:/|$)',  # 目录遍历
]

# 敏感文件名/路径。公开工具默认禁止读取、覆盖或删除这些文件，避免
# Agent 把凭据、私钥、npm/pypi token 等内容暴露到对话或日志中。
SENSITIVE_FILE_PATTERNS = [
    r'(?:^|/)\.git/config$',
    r'(?:^|/)\.ssh(?:/|$)',
    r'(?:^|/)\.env(?:\.(?!(?:example|sample|template|dist)$)[^/]*)?$',
    r'(?:^|/)(?!(?:example|sample|template|dist)\.env$)[^/]*\.env$',
    r'(?:^|/)(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:\.pub)?$',
    r'\.(?:pem|p12|pfx)$',
    r'(?:^|/)\.(?:npmrc|pypirc|netrc)$',
    r'(?:^|/)(?:credentials|secrets?|tokens?)(?:\.[^/]*)?$',
]

# 危险文件扩展名
DANGEROUS_EXTENSIONS = [
    '.exe', '.bat', '.cmd', '.msi', '.dll',
    '.sh', '.bash', '.ps1', '.vbs',
]


def _normalize_path_for_match(path: str) -> str:
    """将路径归一化成适合跨平台正则检查的形式。"""
    return str(path).replace("\\", "/").lower()


def _matches_any_path_pattern(path: str, patterns: List[str]) -> Tuple[bool, str]:
    """检查路径是否命中任一保护正则。"""
    normalized = _normalize_path_for_match(path)
    for pattern in patterns:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return True, pattern
    return False, ""


def check_sensitive_file(path: str) -> Tuple[bool, str]:
    """
    检查路径是否指向敏感文件。

    Returns:
        (是否安全, 原因描述)
    """
    matched, pattern = _matches_any_path_pattern(path, SENSITIVE_FILE_PATTERNS)
    if matched:
        return False, f"操作目标疑似包含敏感凭据或私钥: {pattern}"
    return True, "文件不在敏感文件列表中"


# ==============================================================================
# 数据结构
# ==============================================================================

@dataclass
class SafetyResult:
    """安全检查结果"""
    is_safe: bool
    is_dangerous: bool
    reason: str
    severity: str = "normal"  # normal, warning, danger
    
    def __bool__(self) -> bool:
        return self.is_safe and not self.is_dangerous


# ==============================================================================
# 安全检查函数
# ==============================================================================

def check_file_danger(path: str) -> Tuple[bool, str]:
    """
    检查文件操作是否危险
    
    Args:
        path: 文件路径
        
    Returns:
        (是否安全, 原因描述)
    """
    path_obj = Path(path)

    is_sensitive, sensitive_reason = check_sensitive_file(str(path_obj))
    if not is_sensitive:
        return False, sensitive_reason
    
    # 检查危险路径
    matched, dangerous_pattern = _matches_any_path_pattern(str(path_obj), DANGEROUS_PATH_PATTERNS)
    if matched:
        return False, f"操作目标包含系统保护路径: {dangerous_pattern}"
    
    # 检查危险扩展名
    if path_obj.suffix.lower() in DANGEROUS_EXTENSIONS:
        return False, f"操作目标为可执行文件: {path_obj.suffix}"
    
    # 检查是否在危险目录中
    try:
        resolved = path_obj.resolve()
        is_sensitive, sensitive_reason = check_sensitive_file(str(resolved))
        if not is_sensitive:
            return False, sensitive_reason

        matched, dangerous_pattern = _matches_any_path_pattern(str(resolved), DANGEROUS_PATH_PATTERNS)
        if matched:
            return False, f"操作目标位于系统保护目录: {dangerous_pattern}"
    except (PermissionError, OSError):
        # 如果没有权限解析路径，假设是系统目录
        return False, "操作目标在系统保护目录"
    
    # 检查删除操作
    try:
        if path_obj.exists():
            try:
                if path_obj.is_dir():
                    # 检查是否包含大量文件
                    try:
                        file_count = len(list(path_obj.rglob('*')))
                        if file_count > 100:
                            return False, f"目录包含 {file_count} 个文件，批量删除存在风险"
                    except PermissionError:
                        # 如果没有权限访问目录，假设是系统目录
                        return False, "操作目标在系统保护目录"
            except PermissionError:
                # 如果没有权限检查，假设是系统目录
                return False, "操作目标在系统保护目录"
    except PermissionError:
        # 如果没有权限检查文件是否存在，假设是系统目录
        return False, "操作目标在系统保护目录"
    
    return True, "文件操作安全"


def check_command_danger(command: str) -> Tuple[bool, str]:
    """
    检查命令是否危险
    
    Args:
        command: 要检查的命令
        
    Returns:
        (是否安全, 原因描述)
    """
    if not command or not command.strip():
        return False, "空命令无效"
    
    command_lower = command.lower()
    
    # 检查危险模式
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command_lower):
            return False, f"检测到危险命令模式: {pattern}"
    
    # 检查危险关键词
    danger_keywords = [
        'format', 'fdisk', 'mkfs',
        'dd if=', 'shred',
        ':(){ :|:& };:',  # Fork炸弹
    ]
    
    for keyword in danger_keywords:
        if keyword in command_lower:
            return False, f"检测到危险关键词: {keyword}"
    
    # 检查网络下载并执行
    if 'http://' in command or 'https://' in command:
        if '|' in command or '>' in command or 'sh' in command_lower or 'bash' in command_lower:
            return False, "检测到从网络下载并执行内容的危险操作"
    
    return True, "命令安全"


def check_batch_operation(files: List[str], operation: str) -> Tuple[bool, str]:
    """
    检查批量操作是否危险
    
    Args:
        files: 文件列表
        operation: 操作类型（delete, execute, modify）
        
    Returns:
        (是否安全, 原因描述)
    """
    if not files:
        return True, "无文件需要操作"
    
    # 检查文件数量
    if len(files) > 50:
        return False, f"批量操作涉及 {len(files)} 个文件，超过安全阈值"
    
    # 检查是否有系统文件
    for file_path in files:
        is_safe, reason = check_file_danger(file_path)
        if not is_safe:
            return False, f"批量操作中发现危险文件: {reason}"
    
    # 批量删除检查
    if operation.lower() in ['delete', 'rm', 'del']:
        if len(files) > 10:
            return False, f"批量删除 {len(files)} 个文件需要确认"
    
    return True, "批量操作安全"


def get_danger_level(description: str) -> str:
    """
    根据描述获取危险等级
    
    Args:
        description: 操作描述
        
    Returns:
        危险等级 (low, medium, high, critical)
    """
    description_lower = description.lower()
    
    if '系统' in description or 'format' in description_lower:
        return 'critical'
    elif '递归删除' in description or 'batch' in description_lower:
        return 'high'
    elif '网络下载' in description:
        return 'medium'
    else:
        return 'low'


def sanitize_path(path: str, base_dir: Path = None) -> Path:
    """
    规范化并验证路径，防止目录遍历攻击
    
    Args:
        path: 输入路径
        base_dir: 基础目录（用于限制范围）
        
    Returns:
        规范化后的安全路径
        
    Raises:
        ValueError: 如果路径不安全
    """
    raw_path = Path(path).expanduser()

    # 指定了工作区时，相对路径必须先锚定到工作区，再做规范化。
    if base_dir is not None:
        base_dir = Path(base_dir).expanduser().resolve()
        path_obj = (base_dir / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
    else:
        path_obj = raw_path.resolve()

    # 如果指定了基础目录，确保路径在其范围内
    if base_dir:
        try:
            path_obj.relative_to(base_dir)
        except ValueError:
            raise ValueError(f"路径 '{path}' 不在允许的目录 '{base_dir}' 内")
    
    # 检查路径是否包含危险模式
    path_str = str(path_obj)
    is_sensitive, sensitive_reason = check_sensitive_file(path_str)
    if not is_sensitive:
        raise ValueError(sensitive_reason)

    matched, pattern = _matches_any_path_pattern(path_str, DANGEROUS_PATH_PATTERNS)
    if matched:
        raise ValueError(f"路径包含禁止的模式: {pattern}")
    
    return path_obj


def check_write_operation(file_path: str) -> Tuple[bool, str]:
    """
    检查写入操作是否安全
    
    Args:
        file_path: 文件路径
        
    Returns:
        (是否安全, 原因描述)
    """
    path = Path(file_path)

    is_sensitive, sensitive_reason = check_sensitive_file(str(path))
    if not is_sensitive:
        return False, sensitive_reason
    
    # 检查父目录是否存在且可写
    parent = path.parent
    if not parent.exists():
        # 如果父目录不存在，检查是否尝试创建危险目录
        parts = path.parts
        for i in range(len(parts)):
            partial = Path(*parts[:i+1])
            if partial.name in ['Windows', 'System32', 'etc', 'bin', 'sbin']:
                return False, "禁止在系统目录中创建文件"
    
    # 检查是否覆盖系统文件
    if path.exists():
        is_safe, reason = check_file_danger(str(path))
        if not is_safe:
            return False, f"写入操作目标存在风险: {reason}"
    
    return True, "写入操作安全"


def filter_dangerous_chars(text: str) -> str:
    """
    过滤文本中的危险字符
    
    Args:
        text: 输入文本
        
    Returns:
        过滤后的文本
    """
    # 移除危险的命令分隔符
    dangerous_chars = ['`', '$()', '${}', '|', ';', '&&', '||']
    
    result = text
    for char in dangerous_chars:
        result = result.replace(char, '')
    
    return result


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'SafetyResult',
    'check_file_danger',
    'check_command_danger',
    'check_batch_operation',
    'get_danger_level',
    'sanitize_path',
    'check_write_operation',
    'check_sensitive_file',
    'filter_dangerous_chars',
    'DANGEROUS_COMMAND_PATTERNS',
    'DANGEROUS_PATH_PATTERNS',
    'DANGEROUS_EXTENSIONS',
]
