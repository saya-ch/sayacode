"""
安全确认系统

提供操作前的安全检查和用户确认功能。

功能：
- 文件操作安全检查
- 命令执行安全检查
- 批量操作安全检查
- 危险操作确认
- 自定义确认回调
"""

from typing import Tuple, Optional, Callable, List
from dataclasses import dataclass
from pathlib import Path
import re

from ..i18n import tr

# 导入安全工具
from ..tools.safety import (
    check_file_danger,
    check_command_danger,
    check_batch_operation,
    SafetyResult,
    DANGEROUS_COMMAND_PATTERNS,
    DANGEROUS_PATH_PATTERNS,
    DANGEROUS_EXTENSIONS,
)


# ==============================================================================
# 安全级别定义
# ==============================================================================

class SafetyLevel:
    """安全级别常量"""
    SAFE = "safe"
    LOW_RISK = "low_risk"
    MEDIUM_RISK = "medium_risk"
    HIGH_RISK = "high_risk"
    CRITICAL = "critical"


# ==============================================================================
# 操作记录
# ==============================================================================

@dataclass
class Operation:
    """操作记录"""
    type: str  # file, command, batch
    target: str  # 操作目标
    details: str = ""
    timestamp: str = ""
    confirmed: bool = False


class SafetyChecker:
    """
    安全检查器
    
    在执行危险操作前进行检查和确认：
    - 文件操作检查
    - 命令执行检查
    - 批量操作检查
    - 危险操作确认
    """
    
    def __init__(
        self,
        callback_confirm: Optional[Callable[[str, str], bool]] = None,
        auto_block_critical: bool = True,
        allow_dangerous_commands: bool = False,
        workspace_root: Optional[Path] = None
    ):
        """
        初始化安全检查器
        
        Args:
            callback_confirm: 确认回调函数，接收 (操作描述, 危险原因)，返回用户是否确认
            auto_block_critical: 是否自动阻止危险操作
            allow_dangerous_commands: 是否允许危险命令执行（默认否）
            workspace_root: 工作区根目录（用于路径限制）
        """
        self.callback_confirm = callback_confirm
        self.auto_block_critical = auto_block_critical
        self.allow_dangerous_commands = allow_dangerous_commands
        self.workspace_root = workspace_root or Path.cwd()
        
        # 操作历史
        self.operation_history: List[Operation] = []
        
        # 危险操作白名单（相对安全的操作）
        self.whitelist = {
            'mkdir', 'touch', 'cp', 'copy',
            'cat', 'head', 'tail', 'grep', 'find',
            'git', 'npm', 'pip', 'python',
        }
    
    # =========================================================================
    # 基础检查方法
    # =========================================================================
    
    def check_and_confirm(
        self,
        operation: str,
        target: str,
        danger_level: str = SafetyLevel.LOW_RISK
    ) -> bool:
        """
        检查操作并请求确认
        
        Args:
            operation: 操作类型
            target: 操作目标
            danger_level: 危险等级
        
        Returns:
            用户是否确认执行
        """
        # 自动阻止危险操作
        if self.auto_block_critical and danger_level == SafetyLevel.CRITICAL:
            return False
        
        # 调用确认回调
        if self.callback_confirm:
            return self.callback_confirm(operation, target)
        
        # 默认拒绝
        return False
    
    def check_file_operation(
        self,
        operation: str,
        path: str
    ) -> Tuple[bool, str]:
        """
        检查文件操作的安全性
        
        Args:
            operation: 操作类型 (read, write, delete, execute)
            path: 文件路径
        
        Returns:
            (是否安全, 原因描述)
        """
        # 基本检查
        if not path:
            return False, "路径不能为空"
        
        path_obj = Path(path)
        
        # 检查系统目录
        for dangerous_pattern in DANGEROUS_PATH_PATTERNS:
            if dangerous_pattern.lower() in str(path_obj).lower():
                return False, f"操作目标在系统保护目录: {dangerous_pattern}"
        
        # 检查危险扩展名
        if operation in ['execute', 'run']:
            if path_obj.suffix.lower() in DANGEROUS_EXTENSIONS:
                return False, f"禁止执行危险类型的文件: {path_obj.suffix}"
        
        # 删除操作检查
        if operation == 'delete':
            is_safe, reason = check_file_danger(str(path_obj))
            if not is_safe:
                return False, reason
            
            # 检查是否为空目录
            if path_obj.is_dir():
                try:
                    contents = list(path_obj.iterdir())
                    if len(contents) > 20:
                        return False, f"目录包含 {len(contents)} 个项目，批量删除存在风险"
                except OSError:
                    # 静默忽略：无法列出目录内容，跳过空目录检查
                    pass

        # 写入操作检查
        if operation == 'write':
            # 检查父目录
            parent = path_obj.parent
            for dangerous_pattern in DANGEROUS_PATH_PATTERNS:
                if dangerous_pattern.lower() in str(parent).lower():
                    return False, "禁止在系统目录中创建文件"
        
        return True, "文件操作安全"
    
    def check_command(self, command: str) -> Tuple[bool, str]:
        """
        检查命令执行的安全性
        
        Args:
            command: 要执行的命令
        
        Returns:
            (是否安全, 原因描述)
        """
        if not command or not command.strip():
            return False, "空命令无效"
        
        # 使用安全工具检查
        is_safe, reason = check_command_danger(command)
        
        if not is_safe:
            if not self.allow_dangerous_commands:
                return False, reason
        
        # 检查是否在白名单中
        cmd_parts = command.strip().split()
        if cmd_parts:
            cmd_name = cmd_parts[0].lower()
            if cmd_name in self.whitelist:
                return True, f"命令在白名单中: {cmd_name}"
        
        return True, "命令安全" if is_safe else f"警告: {reason}"
    
    def check_batch(
        self,
        files: List[str],
        operation: str
    ) -> Tuple[bool, str]:
        """
        检查批量操作的安全性
        
        Args:
            files: 文件列表
            operation: 操作类型
        
        Returns:
            (是否安全, 原因描述)
        """
        if not files:
            return True, "无文件需要操作"
        
        # 检查数量限制
        if len(files) > 100:
            return False, f"批量操作涉及 {len(files)} 个文件，超过安全阈值 (100)"
        
        # 使用安全工具检查
        return check_batch_operation(files, operation)
    
    # =========================================================================
    # 高级检查方法
    # =========================================================================
    
    def check_write_to_protected_file(self, file_path: str) -> Tuple[bool, str]:
        """
        检查是否尝试写入受保护文件
        
        Args:
            file_path: 文件路径
        
        Returns:
            (是否安全, 原因描述)
        """
        protected_patterns = [
            r'\.env$',  # 环境配置文件
            r'\.git/config$',
            r'\.ssh/',
            r'/etc/passwd',
            r'/etc/shadow',
            r'/etc/sudoers',
        ]
        
        path_str = str(file_path).lower()
        
        for pattern in protected_patterns:
            if re.search(pattern, path_str):
                return False, f"禁止修改受保护的文件: {pattern}"
        
        return True, "文件不在受保护列表中"
    
    def check_path_traversal(self, path: str) -> Tuple[bool, str]:
        """
        检查是否存在路径遍历攻击
        
        Args:
            path: 文件路径
        
        Returns:
            (是否安全, 原因描述)
        """
        # 检查目录遍历模式
        traversal_patterns = ['../', '..\\', '%2e%2e', '%252e']
        
        path_lower = path.lower()
        
        for pattern in traversal_patterns:
            if pattern in path_lower:
                return False, f"检测到目录遍历模式: {pattern}"
        
        # 检查绝对路径是否超出工作区
        if Path(path).is_absolute() and self.workspace_root:
            try:
                Path(path).relative_to(self.workspace_root.resolve())
            except ValueError:
                return False, f"路径超出工作区范围: {path}"
        
        return True, "路径安全"
    
    def get_operation_risk_level(
        self,
        operation: str,
        target: str
    ) -> str:
        """
        获取操作的危险等级
        
        Args:
            operation: 操作类型
            target: 操作目标
        
        Returns:
            危险等级
        """
        # 系统文件操作
        for pattern in DANGEROUS_PATH_PATTERNS:
            if pattern.lower() in target.lower():
                return SafetyLevel.CRITICAL
        
        # 危险命令
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if re.search(pattern, target.lower()):
                return SafetyLevel.CRITICAL
        
        # 删除操作
        if operation == 'delete':
            is_safe, reason = check_file_danger(target)
            if not is_safe:
                return SafetyLevel.HIGH_RISK
        
        # 批量操作
        if operation == 'batch_delete':
            return SafetyLevel.HIGH_RISK
        
        # 执行操作
        if operation == 'execute':
            path = Path(target)
            if path.suffix.lower() in DANGEROUS_EXTENSIONS:
                return SafetyLevel.HIGH_RISK
        
        # 写入操作
        if operation == 'write':
            is_safe, reason = self.check_write_to_protected_file(target)
            if not is_safe:
                return SafetyLevel.MEDIUM_RISK
        
        return SafetyLevel.SAFE
    
    def generate_warning_message(
        self,
        operation: str,
        target: str,
        danger_level: str
    ) -> str:
        """
        生成警告消息

        Args:
            operation: 操作类型
            target: 操作目标
            danger_level: 危险等级

        Returns:
            格式化的警告消息
        """
        lines = []

        lines.append("=" * 50)
        lines.append(f"⚠️ {tr('safety.warning.title')} ⚠️")
        lines.append("=" * 50)
        lines.append("")

        lines.append(tr("safety.warning.operation", operation=operation))
        lines.append(tr("safety.warning.target", target=target))
        lines.append(tr("safety.warning.danger_level", level=danger_level))
        lines.append("")

        if danger_level == SafetyLevel.CRITICAL:
            lines.extend(tr("safety.warning.critical").split("\n"))
        elif danger_level == SafetyLevel.HIGH_RISK:
            lines.extend(tr("safety.warning.high_risk").split("\n"))
        elif danger_level == SafetyLevel.MEDIUM_RISK:
            lines.extend(tr("safety.warning.medium_risk").split("\n"))
        else:
            lines.append(tr("safety.warning.safe"))

        lines.append("")
        lines.append("=" * 50)

        return "\n".join(lines)
    def request_confirmation(
        self,
        operation: str,
        target: str,
        warning_message: str = None
    ) -> bool:
        """
        请求用户确认
        
        Args:
            operation: 操作类型
            target: 操作目标
            warning_message: 自定义警告消息
        
        Returns:
            用户是否确认
        """
        # 获取危险等级
        danger_level = self.get_operation_risk_level(operation, target)
        
        # 生成警告消息
        if warning_message is None:
            warning_message = self.generate_warning_message(operation, target, danger_level)
        
        # 自动阻止危险操作
        if danger_level == SafetyLevel.CRITICAL and self.auto_block_critical:
            print(warning_message)
            return False
        
        # 打印警告
        print(warning_message)
        
        # 调用确认回调
        if self.callback_confirm:
            return self.callback_confirm(operation, target)
        
        return False
    
    def log_operation(
        self,
        operation_type: str,
        target: str,
        details: str = "",
        confirmed: bool = False
    ):
        """
        记录操作到历史
        
        Args:
            operation_type: 操作类型
            target: 操作目标
            details: 详细信息
            confirmed: 是否已确认
        """
        from datetime import datetime, timezone
        
        operation = Operation(
            type=operation_type,
            target=target,
            details=details,
            timestamp=datetime.now(timezone.utc).isoformat(),
            confirmed=confirmed
        )
        
        self.operation_history.append(operation)
        
        # 保持历史长度限制
        if len(self.operation_history) > 1000:
            self.operation_history = self.operation_history[-500:]
    
    def get_operation_history(
        self,
        operation_type: Optional[str] = None,
        limit: int = 100
    ) -> List[Operation]:
        """
        获取操作历史
        
        Args:
            operation_type: 过滤操作类型
            limit: 返回数量限制
        
        Returns:
            操作记录列表
        """
        history = self.operation_history
        
        if operation_type:
            history = [op for op in history if op.type == operation_type]
        
        return history[-limit:]
    
    def clear_history(self):
        """清空操作历史"""
        self.operation_history = []


# ==============================================================================
# 便捷函数
# ==============================================================================

def quick_check_file(path: str) -> Tuple[bool, str]:
    """快速检查文件操作安全性"""
    checker = SafetyChecker()
    return checker.check_file_operation("read", path)


def quick_check_command(command: str) -> Tuple[bool, str]:
    """快速检查命令安全性"""
    checker = SafetyChecker()
    return checker.check_command(command)


def create_safe_checker(
    callback: Optional[Callable[[str, str], bool]] = None
) -> SafetyChecker:
    """
    创建安全检查器（带确认回调）
    
    Args:
        callback: 确认回调函数
    
    Returns:
        SafetyChecker 实例
    """
    return SafetyChecker(callback_confirm=callback)


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'SafetyChecker',
    'SafetyLevel',
    'SafetyResult',
    'Operation',
    'quick_check_file',
    'quick_check_command',
    'create_safe_checker',
]