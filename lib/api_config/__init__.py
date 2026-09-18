"""API 配置包入口。

职责是汇出多 provider 接入配置：`api_config` 管配置的存取与校验，
`wizard` 管交互式引导流程。核心类为 `APIConfig` / `APIConfigManager` /
`APIConfigWizard`，由 CLI 配置命令与启动流程调用。
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
