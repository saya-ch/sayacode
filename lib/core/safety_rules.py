"""
底层安全规则：纯函数，无包内依赖，core 与 tools 共用。

提供文件操作、命令执行的危险检测功能，防止意外或恶意的危险操作。

危险操作包括：
- 删除系统文件或递归删除目录
- 格式化操作
- 修改系统目录
- 执行未知来源的可执行文件
"""

import re
from pathlib import Path
from typing import Any, List, Optional, Tuple
from dataclasses import dataclass


# 维护危险命令与路径模式，供执行前拦截。
DANGEROUS_COMMAND_PATTERNS = [
    # 拦截递归强制删除命令。
    r'\brm\s+-[a-z]*r[a-z]*\b',
    r'\brm\s+--recursive\b',
    r'rm\s+-rf\s+',
    r'rm\s+-\s*r\s+-\s*f',
    r'rm\s+-r\s+-f\b',
    r'rm\s+-f\s+-r\b',
    r'del\s+/s\s+/q',
    r'del\s+\/s\s+\/q',
    r'(?:rmdir|rd)\s+/s\b',
    r'(?:rmdir|rd)\s+/s\s+/q',
    r'rm\s+-rf\b',
    r'remove-item\b.*-recurse\b',
    r'remove-item\b.*-force\b.*-recurse\b',
    r'remove-item\b.*-recurse\b.*-force\b',
    r'\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*(?:-|/)(?:enc|encodedcommand)\b',
    
    # 拦截格式化命令，避免清空磁盘。
    r'format\s+',
    r'format\b',
    
    # 拦截危险网络下载执行操作。
    r'curl\s+.*\|\s*sh',
    r'wget\s+.*\|\s*sh',
    r'sh\s+<.*http',
    
    # 拦截系统目录修改操作。
    r'sudo\s+.*\s+rm\s+',
    r'sudo\s+.*\s+del\s+',
    r'\.\./\.\./',  # 拦截目录遍历攻击。
    
    # 拦截危险文件执行操作。
    r'\|\s*sh\b',
    r'exec\s+',
]

# 列举系统保护路径模式，供执行前拦截。
DANGEROUS_PATH_PATTERNS = [
    r'^[a-z]:/windows(?:/|$)',
    r'^[a-z]:/program files(?: \(x86\))?(?:/|$)',
    r'^[a-z]:/system(?:/|$)',
    r'^/(?:etc|bin|sbin|usr/bin|usr/sbin|root)(?:/|$)',
    r'^~(?:/|$)',
    r'(?:^|/)\.\.(?:/|$)',  # 拦截目录遍历攻击。
]

# 禁止访问敏感文件，避免凭据私钥泄露到对话或日志。
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

# 列举危险可执行扩展名，供执行前拦截。
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

    返回:
        (是否安全, 原因描述)
    """
    matched, pattern = _matches_any_path_pattern(path, SENSITIVE_FILE_PATTERNS)
    if matched:
        return False, f"操作目标疑似包含敏感凭据或私钥: {pattern}"
    return True, "文件不在敏感文件列表中"


# 定义安全检查结果数据结构。

@dataclass
class SafetyResult:
    """安全检查结果（生产零消费，仅测试与兼容保留）。"""
    is_safe: bool
    is_dangerous: bool
    reason: str
    severity: str = "normal"  # 限定取值为 normal、warning 与 danger 三档。
    
    def __bool__(self) -> bool:
        return self.is_safe and not self.is_dangerous


# 提供文件、命令与批量操作安全检查。

def check_file_danger(path: str) -> Tuple[bool, str]:
    """
    检查路径本身是否有风险（敏感文件 / 受保护目录 / 危险扩展名）。

    参数:
        path: 文件路径

    返回:
        (是否安全, 原因描述)

    本函数与操作类型无关，目录规模判断是删除专有判据，不在这里做。
    否则只读的目录列举会被误拦并报批量删除风险。
    删除路径请另外调用删除检查。
    """
    path_obj = Path(path)

    is_sensitive, sensitive_reason = check_sensitive_file(str(path_obj))
    if not is_sensitive:
        return False, sensitive_reason
    
    # 检查危险路径，命中则直接拒绝。
    matched, dangerous_pattern = _matches_any_path_pattern(str(path_obj), DANGEROUS_PATH_PATTERNS)
    if matched:
        return False, f"操作目标包含系统保护路径: {dangerous_pattern}"
    
    # 检查危险扩展名，命中则直接拒绝。
    if path_obj.suffix.lower() in DANGEROUS_EXTENSIONS:
        return False, f"操作目标为可执行文件: {path_obj.suffix}"
    
    # 检查是否位于危险目录，命中则直接拒绝。
    try:
        resolved = path_obj.resolve()
        is_sensitive, sensitive_reason = check_sensitive_file(str(resolved))
        if not is_sensitive:
            return False, sensitive_reason

        matched, dangerous_pattern = _matches_any_path_pattern(str(resolved), DANGEROUS_PATH_PATTERNS)
        if matched:
            return False, f"操作目标位于系统保护目录: {dangerous_pattern}"
    except (PermissionError, OSError):
        # 无权限解析路径时视为系统目录，直接拒绝。
        return False, "操作目标在系统保护目录"
    
    return True, "文件操作安全"


def check_delete_danger(path: str) -> Tuple[bool, str]:
    """检查删除操作是否危险 —— 只有删除路径才应该调用。

    当前判据：目标是目录且递归条目超过 100 时拒绝（避免一条指令删掉整棵树）。

    单独成函数是刻意的：该判据只对删除成立。放在 check_file_danger
    里会让所有调用方（包括只读的 list_directory）都继承它。
    """
    path_obj = Path(path)
    try:
        if not path_obj.exists():
            return True, "删除目标不存在"
        if not path_obj.is_dir():
            return True, "删除目标不是目录"

        try:
            file_count = len(list(path_obj.rglob('*')))
        except PermissionError:
            # 无权限访问目录时视为系统目录，直接拒绝。
            return False, "操作目标在系统保护目录"

        if file_count > 100:
            return False, f"目录包含 {file_count} 个文件，批量删除存在风险"
    except PermissionError:
        # 无权限检查存在性时视为系统目录，直接拒绝。
        return False, "操作目标在系统保护目录"

    return True, "删除操作安全"

def check_command_danger(command: str) -> Tuple[bool, str]:
    """
    检查命令是否危险
    
    参数:
        command: 要检查的命令
        
    返回:
        (是否安全, 原因描述)
    """
    if not command or not command.strip():
        return False, "空命令无效"
    
    command_lower = command.lower()
    
    # 检查危险命令模式，命中则直接拒绝。
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command_lower):
            return False, f"检测到危险命令模式: {pattern}"
    
    # 检查危险关键词，命中则直接拒绝。
    danger_keywords = [
        'format', 'fdisk', 'mkfs',
        'dd if=', 'shred',
        ':(){ :|:& };:',  # 拦截 Fork 炸弹攻击。
    ]
    
    for keyword in danger_keywords:
        if keyword in command_lower:
            return False, f"检测到危险关键词: {keyword}"
    
    # 检查网络下载执行操作，命中则直接拒绝。
    if 'http://' in command or 'https://' in command:
        if '|' in command or '>' in command or 'sh' in command_lower or 'bash' in command_lower:
            return False, "检测到从网络下载并执行内容的危险操作"
    
    return True, "命令安全"


def check_batch_operation(files: List[str], operation: str) -> Tuple[bool, str]:
    """
    检查批量操作是否危险
    
    参数:
        files: 文件列表
        operation: 操作类型（delete, execute, modify）
        
    返回:
        (是否安全, 原因描述)
    """
    if not files:
        return True, "无文件需要操作"
    
    # 检查批量文件数量，超限则直接拒绝。
    if len(files) > 50:
        return False, f"批量操作涉及 {len(files)} 个文件，超过安全阈值"
    
    # 检查是否包含系统文件，命中则直接拒绝。
    for file_path in files:
        is_safe, reason = check_file_danger(file_path)
        if not is_safe:
            return False, f"批量操作中发现危险文件: {reason}"
    
    # 检查批量删除规模，超限则直接拒绝。
    if operation.lower() in ['delete', 'rm', 'del']:
        if len(files) > 10:
            return False, f"批量删除 {len(files)} 个文件需要确认"

        # 拦截大目录删除，check_file_danger 仅校验路径本身。
        for file_path in files:
            is_safe, reason = check_delete_danger(file_path)
            if not is_safe:
                return False, reason

    return True, "批量操作安全"


def get_danger_level(description: str) -> str:
    """
    根据描述获取危险等级（生产零消费，仅测试与兼容保留）

    参数:
        description: 操作描述

    返回:
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


def sanitize_path(path: str, base_dir: Optional[Path] = None) -> Path:
    """
    规范化并验证路径，防止目录遍历攻击
    
    参数:
        path: 输入路径
        base_dir: 基础目录（用于限制范围）
        
    返回:
        规范化后的安全路径
        
    异常:
        ValueError: 如果路径不安全
    """
    raw_path = Path(path).expanduser()

    # 锚定相对路径到工作区，再做规范化处理。
    if base_dir is not None:
        base_dir = Path(base_dir).expanduser().resolve()
        path_obj = (base_dir / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
    else:
        path_obj = raw_path.resolve()

    # 约束路径在基础目录内，越界则直接拒绝。
    if base_dir:
        try:
            path_obj.relative_to(base_dir)
        except ValueError:
            raise ValueError(f"路径 '{path}' 不在允许的目录 '{base_dir}' 内")
    
    # 检查路径危险模式，命中则直接拒绝。
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
    
    参数:
        file_path: 文件路径
        
    返回:
        (是否安全, 原因描述)
    """
    path = Path(file_path)

    is_sensitive, sensitive_reason = check_sensitive_file(str(path))
    if not is_sensitive:
        return False, sensitive_reason
    
    # 检查父目录存在性与可写性，缺失则进一步校验。
    parent = path.parent
    if not parent.exists():
        # 拦截在系统目录下创建文件的操作。
        parts = path.parts
        for i in range(len(parts)):
            partial = Path(*parts[:i+1])
            if partial.name in ['Windows', 'System32', 'etc', 'bin', 'sbin']:
                return False, "禁止在系统目录中创建文件"
    
    # 检查是否覆盖系统文件，命中则直接拒绝。
    if path.exists():
        is_safe, reason = check_file_danger(str(path))
        if not is_safe:
            return False, f"写入操作目标存在风险: {reason}"
    
    return True, "写入操作安全"


def filter_dangerous_chars(text: str) -> str:
    """
    过滤文本中的危险字符

    注意：生产路径零调用（命令拦截走 check_command_danger），仅测试与兼容保留。

    参数:
        text: 输入文本

    返回:
        过滤后的文本
    """
    # 先处理内嵌形式 $(...) / ${...}（含 $(c)/${e}），再处理残留分隔符。
    result = re.sub(r'\$\([^)]*\)', '', text)
    result = re.sub(r'\$\{[^}]*\}', '', result)
    # 移除危险命令分隔符，净化输入文本。
    for char in ['`', '$()', '${}', '|', ';', '&&', '||']:
        result = result.replace(char, '')

    return result


# 导出公共安全检查能力。

# 工具参数中可能承载待检目标的键。command 单独优先，其余按文件目标处理。
_SAFETY_COMMAND_KEYS = ("command",)
_SAFETY_FILE_KEYS = ("path", "file_path", "file", "directory", "dir", "target")


def find_safety_target(args: Any, extra_file_keys: tuple = ()) -> Any:
    """从工具参数中提取待检目标，拿不到返回空。

    唯一原语，图内否决与调用前复检共用，键枚举在此收敛。
    额外文件键供多一个工作目录键的调用方扩展。
    """
    if not isinstance(args, dict):
        return None
    for key in _SAFETY_COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return ("command", value)
    for key in (*_SAFETY_FILE_KEYS, *extra_file_keys):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return ("file", value)
    return None


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
    'BLOCKED_MARKERS',
    'SIBLING_ABORT_TOOLS',
    'is_blocked_result',
]


# 和工具结果文本相关的共享表：拒绝只认首行警告前缀加下面任一标记，
# 正文深处提到拒绝字样不算拒绝。中间件和工具包裹器共用同一张表。
BLOCKED_MARKERS = (
    "Permission required for tool",
    "Permission denied for tool",
    "安全检查失败",
    "安全警告",
    "危险操作已阻止",
    "工作目录不安全",
    "操作已中止",
    "Hook '",
)

# 失败时需要向同级广播中止的工具名。
SIBLING_ABORT_TOOLS = frozenset({
    "execute_command_tool",
    "git_add",
    "git_commit",
    "git_push",
    "git_pull",
    "git_checkout",
    "git_stash",
})


def is_blocked_result(result: Any) -> bool:
    """看工具结果是不是以文本形式返回的策略或安全拒绝。"""
    content: Any = result
    if isinstance(content, dict):
        content = content.get("content", "")
    elif not isinstance(content, str) and hasattr(content, "content"):
        content = content.content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        content = "\n".join(parts)
    if not isinstance(content, str):
        return False
    first = ""
    for line in content.strip().splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first or not first.startswith("⚠️"):
        return False
    return any(marker in first for marker in BLOCKED_MARKERS)
