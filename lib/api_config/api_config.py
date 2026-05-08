"""
API 配置模块

提供多 API 接口规范配置功能，支持：
- OpenAI 规范
- Anthropic 规范
- Azure OpenAI 规范
- Ollama 规范
- Generic OpenAI 兼容
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Dict, List
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from ..core.private_io import ensure_private_dir, write_private_json
from ..core.paths import SayacodePaths
from ..i18n import tr
from ..models.base import parse_context_window
from ..models.provider_catalog import provider_catalog_entry


API_CONFIG_SCHEMA_VERSION = 2


def _is_local_http_url(url: str) -> bool:
    """Return whether url is HTTP and points at the local machine."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme.lower() != "http":
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return host in {"localhost", "127.0.0.1", "::1"}


class APIType(Enum):
    """API 接口规范类型"""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    AZURE_OPENAI = "azure_openai"
    GOOGLE_GEMINI = "gemini"
    OLLAMA = "ollama"
    GENERIC = "generic"

    @property
    def display_name(self) -> str:
        """获取显示名称"""
        names = {
            APIType.OPENAI: tr("api_type.openai"),
            APIType.ANTHROPIC: tr("api_type.anthropic"),
            APIType.AZURE_OPENAI: tr("api_type.azure_openai"),
            APIType.GOOGLE_GEMINI: tr("api_type.gemini"),
            APIType.OLLAMA: tr("api_type.ollama"),
            APIType.GENERIC: tr("api_type.generic"),
        }
        return names.get(self, self.value)

    @property
    def default_base_url(self) -> str:
        """获取默认 Base URL"""
        return provider_catalog_entry(self.value).resolved_default_base_url()

    @property
    def default_model(self) -> str:
        """获取默认模型名称"""
        return provider_catalog_entry(self.value).default_model_name

    @property
    def endpoint(self) -> str:
        """获取 API 端点"""
        return provider_catalog_entry(self.value).endpoint

    @property
    def requires_api_key(self) -> bool:
        """是否需要 API Key"""
        return provider_catalog_entry(self.value).requires_api_key

    @property
    def api_key_env(self) -> Optional[str]:
        """获取对应的环境变量名称。"""
        return provider_catalog_entry(self.value).api_key_env

    @classmethod
    def from_value(cls, value: str) -> Optional["APIType"]:
        """从字符串值获取 APIType"""
        normalized = str(value or "").lower().strip()
        if normalized == "azure":
            normalized = APIType.AZURE_OPENAI.value
        for api_type in cls:
            if api_type.value == normalized:
                return api_type
        return None


@dataclass
class APIConfig:
    """API 配置"""
    api_type: APIType
    base_url: str
    api_key: str = ""
    model_name: str = ""
    timeout: int = 60
    max_retries: int = 3
    temperature: float = 0.2
    max_tokens: Optional[int] = None
    context_window: Optional[int] = None
    metadata: Dict = field(default_factory=dict)

    # Azure OpenAI 特定配置
    azure_api_version: Optional[str] = None
    azure_deployment: Optional[str] = None

    def __post_init__(self):
        """初始化后处理"""
        if isinstance(self.api_type, str):
            api_type = APIType.from_value(self.api_type)
            if api_type:
                self.api_type = api_type

        if not self.model_name:
            provider_value = self.api_type.value if isinstance(self.api_type, APIType) else self.api_type
            self.model_name = provider_catalog_entry(provider_value).default_model_name

        if self.metadata is None:
            self.metadata = {}

        if self.context_window is not None:
            self.context_window = parse_context_window(self.context_window)

    def to_dict(self) -> Dict:
        """转换为字典"""
        data = asdict(self)
        data['api_type'] = self.api_type.value if isinstance(self.api_type, APIType) else self.api_type
        if isinstance(self.api_type, APIType):
            env_name = self.api_type.api_key_env
            env_value = os.environ.get(env_name, "") if env_name else ""
            if env_name and self.api_key and self.api_key == env_value:
                data["api_key"] = ""
                metadata = dict(data.get("metadata") or {})
                metadata["api_key_env"] = env_name
                data["metadata"] = metadata
        return data

    @classmethod
    def from_dict(cls, data: Dict) -> "APIConfig":
        """从字典创建"""
        data = dict(data)
        if 'api_type' in data and isinstance(data['api_type'], str):
            api_type = APIType.from_value(data['api_type'])
            if api_type:
                data['api_type'] = api_type
        return cls(**data)

    def validate(self) -> tuple[bool, str]:
        """
        验证配置

        Returns:
            (是否有效, 错误消息)
        """
        if not isinstance(self.api_type, APIType):
            return False, tr("api_config.unsupported_type", value=self.api_type)

        # 验证 Base URL
        if not self.base_url:
            return False, tr("api_config.base_url_required")

        try:
            parsed_base_url = urlparse(self.base_url)
        except Exception:
            return False, tr("api_config.base_url_scheme_required")
        scheme = parsed_base_url.scheme.lower()
        if scheme not in {"http", "https"} or not parsed_base_url.netloc:
            return False, tr("api_config.base_url_scheme_required")

        # 本地 Ollama 允许 HTTP
        if self.api_type == APIType.OLLAMA:
            pass  # 允许 http://localhost
        elif scheme == "http" and not _is_local_http_url(self.base_url):
            return False, tr("api_config.https_recommended")

        # 验证 API Key
        env_name = self.api_type.api_key_env if isinstance(self.api_type, APIType) else None
        env_api_key = os.environ.get(env_name, "") if env_name else ""

        default_base_url = self.api_type.default_base_url.rstrip("/")
        actual_base_url = str(self.base_url or "").rstrip("/")
        if (
            self.api_type.requires_api_key
            and actual_base_url == default_base_url
            and not (self.api_key or env_api_key)
        ):
            return False, tr("api_config.api_key_required", provider=self.api_type.display_name)

        # 验证模型名称
        if not self.model_name:
            return False, tr("api_config.model_name_required")

        if self.context_window is not None and self.context_window <= 0:
            return False, tr("api_config.context_window_positive")

        return True, ""


class APIConfigManager:
    """API 配置管理器"""

    def __init__(self, config_dir: Optional[str] = None):
        """
        初始化配置管理器

        Args:
            config_dir: 配置目录路径，默认为用户配置目录
        """
        if config_dir:
            self.config_dir = Path(config_dir)
        else:
            self.config_dir = SayacodePaths.resolve(create=True).home

        ensure_private_dir(self.config_dir)
        self.configs: Dict[str, APIConfig] = {}
        self.configs_file = self.config_dir / "api_configs.json"
        self.current_config_name: Optional[str] = None
        self.legacy_config_detected = False

        # 加载已有配置
        self._load_configs()

    def _load_configs(self):
        """从文件加载配置"""
        if not self.configs_file.exists():
            return

        try:
            with open(self.configs_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("schema_version") != API_CONFIG_SCHEMA_VERSION:
                self.legacy_config_detected = True
                return

            self.configs = {
                name: APIConfig.from_dict(config_data)
                for name, config_data in data.get('configs', {}).items()
            }
            self.current_config_name = data.get('current', None)

        except Exception as e:
            print(tr("api_config.load_failed", error=e))

    def _save_configs(self):
        """保存配置到文件"""
        try:
            data = {
                'schema_version': API_CONFIG_SCHEMA_VERSION,
                'configs': {
                    name: config.to_dict()
                    for name, config in self.configs.items()
                },
                'current': self.current_config_name
            }

            write_private_json(self.configs_file, data)

        except Exception as e:
            print(tr("api_config.save_failed", error=e))

    def add_config(self, name: str, config: APIConfig) -> bool:
        """
        添加配置

        Args:
            name: 配置名称
            config: API 配置

        Returns:
            是否成功
        """
        is_valid, error_msg = config.validate()
        if not is_valid:
            print(tr("api_config.validation_failed", error=error_msg))
            return False

        self.configs[name] = config

        # 如果是第一个配置，自动设为当前
        if self.current_config_name is None:
            self.current_config_name = name

        self._save_configs()
        return True

    def get_config(self, name: str) -> Optional[APIConfig]:
        """
        获取配置

        Args:
            name: 配置名称

        Returns:
            API 配置，如果不存在返回 None
        """
        return self.configs.get(name)

    def get_current_config(self) -> Optional[APIConfig]:
        """
        获取当前配置

        Returns:
            当前 API 配置
        """
        if self.current_config_name:
            return self.configs.get(self.current_config_name)
        return None

    def set_current(self, name: str) -> bool:
        """
        设置当前配置

        Args:
            name: 配置名称

        Returns:
            是否成功
        """
        if name not in self.configs:
            return False

        self.current_config_name = name
        self._save_configs()
        return True

    def list_configs(self) -> List[str]:
        """
        列出所有配置

        Returns:
            配置名称列表
        """
        return list(self.configs.keys())

    def delete_config(self, name: str) -> bool:
        """
        删除配置

        Args:
            name: 配置名称

        Returns:
            是否成功
        """
        if name not in self.configs:
            return False

        del self.configs[name]

        # 如果删除的是当前配置，重置当前配置
        if self.current_config_name == name:
            self.current_config_name = next(iter(self.configs.keys()), None)

        self._save_configs()
        return True

    def rename_config(self, old_name: str, new_name: str) -> bool:
        """
        重命名配置

        Args:
            old_name: 原名称
            new_name: 新名称

        Returns:
            是否成功
        """
        if old_name not in self.configs:
            return False

        if new_name in self.configs:
            return False

        self.configs[new_name] = self.configs.pop(old_name)

        if self.current_config_name == old_name:
            self.current_config_name = new_name

        self._save_configs()
        return True

    def get_config_details(self, name: str) -> Optional[Dict]:
        """
        获取配置详细信息（隐藏 API Key）

        Args:
            name: 配置名称

        Returns:
            配置信息字典
        """
        config = self.configs.get(name)
        if not config:
            return None

        # 隐藏 API Key
        api_key = config.api_key
        masked_key = ""
        if api_key:
            if len(api_key) <= 8:
                masked_key = "***"
            else:
                masked_key = api_key[:4] + "..." + api_key[-4:]

        return {
            "name": name,
            "api_type": config.api_type.value if isinstance(config.api_type, APIType) else config.api_type,
            "api_type_display": config.api_type.display_name if isinstance(config.api_type, APIType) else "",
            "base_url": config.base_url,
            "api_key_masked": masked_key,
            "model_name": config.model_name,
            "timeout": config.timeout,
            "max_retries": config.max_retries,
            "temperature": config.temperature,
            "context_window": config.context_window,
            "is_current": name == self.current_config_name,
        }


# 全局配置管理器实例
_config_manager: Optional[APIConfigManager] = None


def get_api_config_manager() -> APIConfigManager:
    """
    获取全局 API 配置管理器

    Returns:
        APIConfigManager 实例
    """
    global _config_manager
    if _config_manager is None:
        _config_manager = APIConfigManager()
    return _config_manager
