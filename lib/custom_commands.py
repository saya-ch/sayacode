"""兼容 shim：自定义命令已搬至 lib.commands.custom，原路径仅做转发。"""

from lib.commands.custom import (
    CustomCommand as CustomCommand,
    _command_roots as _command_roots,
    _split_frontmatter as _split_frontmatter,
    discover_custom_commands as discover_custom_commands,
    list_custom_commands as list_custom_commands,
    load_project_mcp_config as load_project_mcp_config,
    render_custom_command as render_custom_command,
)

__all__ = [
    "CustomCommand",
    "discover_custom_commands",
    "list_custom_commands",
    "load_project_mcp_config",
    "render_custom_command",
]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证旧引用继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.commands.custom"), name)
