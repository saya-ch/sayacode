"""
配置模块

提供多 API 接口规范配置功能。
"""

from .api_config import (
    API_CONFIG_SCHEMA_VERSION,
    APIType,
    APIConfig,
    APIConfigManager,
    get_api_config_manager,
)

from .wizard import (
    APIConfigWizard,
    APIConfigWizardCLI,
)

__all__ = [
    # 新版 API 配置
    "APIType",
    "API_CONFIG_SCHEMA_VERSION",
    "APIConfig",
    "APIConfigManager",
    "get_api_config_manager",
    # 配置向导
    "APIConfigWizard",
    "APIConfigWizardCLI",
]
