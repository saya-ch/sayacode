"""
SAYACODE CLI 包

按照功能拆分为以下子模块：
- parser:      CLI_VERSION, BUILTIN_COMMANDS, 参数解析, 协议菜单, 语言覆盖
- configure:   模型配置、连接测试、上下文窗口辅助
- workspace:   工作区路径解析、Git 变更检查
- permissions: 交互式权限确认、安全输入
- main:        主入口、用户配置加载/保存
"""

from lib.cli.configure import _get_protocol_option
from lib.cli.parser import (
    CLI_VERSION,
    BUILTIN_COMMANDS,
    PROTOCOL_DEFAULTS,
    PROTOCOL_OPTIONS,
    USER_VISIBLE_MODEL_TYPES,
    LocalizedHelpFormatter,
    build_cli_parser,
)
from lib.cli.main import main

__all__ = [
    "CLI_VERSION",
    "BUILTIN_COMMANDS",
    "PROTOCOL_DEFAULTS",
    "PROTOCOL_OPTIONS",
    "USER_VISIBLE_MODEL_TYPES",
    "LocalizedHelpFormatter",
    "_get_protocol_option",
    "build_cli_parser",
    "main",
]
