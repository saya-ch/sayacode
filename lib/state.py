"""兼容 shim：运行状态已搬至 lib.runtime.state，原路径仅做转发。"""

from lib.runtime.state import (
    SAYACODE_CONFIG_SCHEMA_VERSION,
    AppState,
    ConfigState,
    UserConfig,
    create_app_state,
    create_config_state,
    create_user_config,
)

__all__ = [
    'AppState',
    'ConfigState',
    'UserConfig',
    'SAYACODE_CONFIG_SCHEMA_VERSION',
    'create_app_state',
    'create_config_state',
    'create_user_config',
]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证旧引用继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.runtime.state"), name)
