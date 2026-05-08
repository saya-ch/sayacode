"""
状态管理模块

定义应用程序的状态管理数据结构，用于在 CLI 和 Agent 之间传递状态。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

# 导入核心模块
from .core.session import SessionManager
from .core.memory import MemoryManager
from .core.safety import SafetyChecker
from .core.context import ProjectContext
from .core.private_io import write_private_json
from .core.paths import SayacodePaths
from .i18n import normalize_language
from .prompts import normalize_prompt_style
from .core.modes import normalize_agent_mode


SAYACODE_CONFIG_SCHEMA_VERSION = 2


# ==============================================================================
# 状态类
# ==============================================================================

@dataclass
class AppState:
    """
    应用程序状态
    
    包含所有运行时的状态信息：
    - 工作区路径
    - 模型配置
    - 会话管理
    - 记忆管理
    - 安全检查器
    - 项目上下文
    """
    
    # 基本信息
    workspace: Path
    model_type: str
    model_config: Dict[str, Any]
    
    # 核心管理器
    session: SessionManager
    memory: MemoryManager
    safety: SafetyChecker
    context: Optional[ProjectContext] = None
    
    # 元数据
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    # 配置选项
    auto_save_session: bool = True
    stream_output: bool = True
    confirm_dangerous: bool = True
    active_profile: Optional[str] = None
    restored_session: bool = False
    prompt_style: str = "standard"
    agent_mode: str = "build"
    runtime_context: Optional[Any] = None
    
    def __post_init__(self):
        """初始化后处理"""
        # 确保 workspace 是 Path 对象
        if not isinstance(self.workspace, Path):
            self.workspace = Path(self.workspace)

        self.prompt_style = normalize_prompt_style(self.prompt_style)
        self.agent_mode = normalize_agent_mode(self.agent_mode) or "build"
        
        # 如果有上下文，设置根目录
        if self.context:
            self.context.root_dir = self.workspace
    
    def update(self):
        """更新时间戳"""
        self.last_updated = datetime.now(timezone.utc).isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "workspace": str(self.workspace),
            "model_type": self.model_type,
            "model_config": self.model_config,
            "session_id": self.session.session_id,
            "memory_stats": self.memory.get_stats(),
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "auto_save_session": self.auto_save_session,
            "stream_output": self.stream_output,
            "confirm_dangerous": self.confirm_dangerous,
            "active_profile": self.active_profile,
            "restored_session": self.restored_session,
            "prompt_style": self.prompt_style,
            "agent_mode": self.agent_mode,
        }
    
    def __repr__(self) -> str:
        return (
            f"AppState("
            f"workspace={self.workspace.name}, "
            f"model={self.model_type}, "
            f"session={self.session.session_id}"
            f")"
        )


@dataclass
class ConfigState:
    """
    配置状态
    
    存储应用程序的配置信息。
    """
    
    # 模型配置
    default_model_type: str = "ollama"
    default_model_name: str = "llama3.2"
    ollama_base_url: str = "http://localhost:11434"
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    
    # 界面配置
    theme: str = "sakura"
    show_thinking: bool = True
    show_tool_calls: bool = True
    auto_analyze_project: bool = True
    
    # 安全配置
    auto_block_critical: bool = True
    allow_dangerous_commands: bool = False
    confirm_threshold: str = "medium"
    
    # 记忆配置
    max_history: int = 50
    max_context_length: int = 4096
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "default_model_type": self.default_model_type,
            "default_model_name": self.default_model_name,
            "ollama_base_url": self.ollama_base_url,
            "openai_api_key": "***" if self.openai_api_key else None,
            "openai_base_url": self.openai_base_url,
            "theme": self.theme,
            "show_thinking": self.show_thinking,
            "show_tool_calls": self.show_tool_calls,
            "auto_analyze_project": self.auto_analyze_project,
            "auto_block_critical": self.auto_block_critical,
            "allow_dangerous_commands": self.allow_dangerous_commands,
            "confirm_threshold": self.confirm_threshold,
            "max_history": self.max_history,
            "max_context_length": self.max_context_length,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConfigState":
        """从字典创建"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def save(self, file_path: str):
        """保存配置到文件"""
        write_private_json(file_path, self.to_dict())
    
    @classmethod
    def load(cls, file_path: str) -> Optional["ConfigState"]:
        """从文件加载配置"""
        try:
            if not Path(file_path).exists():
                return None
            
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return cls.from_dict(data)
        except Exception:
            return None


@dataclass
class UserConfig:
    """
    用户级偏好配置。

    用于持久化 CLI 的默认工作区、当前激活 profile 名称以及交互偏好。
    """

    workspace: Optional[str] = None
    active_profile: Optional[str] = None
    model_type: Optional[str] = None
    model_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    stream_output: bool = True
    confirm_dangerous: bool = True
    language: str = "auto"
    prompt_style: str = "standard"
    agent_mode: str = "build"
    show_startup_guide: bool = True
    onboarding_completed: bool = False
    last_used_at: Optional[str] = None

    @classmethod
    def default_path(cls) -> Path:
        """返回默认的用户配置文件路径。"""
        return SayacodePaths.resolve(create=True).user_config

    @staticmethod
    def _sanitize_base_url(base_url: Optional[str]) -> Optional[str]:
        """过滤损坏或非 URL 形式的 base_url，避免脏数据污染默认配置。"""
        if base_url is None:
            return None

        value = str(base_url).replace("\ufeff", "").strip()
        if not value or any(char in value for char in ("\r", "\n", "\t")):
            return None

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None

        return value

    def to_dict(self) -> Dict[str, Any]:
        """转换为用于持久化的字典。"""
        return {
            "schema_version": SAYACODE_CONFIG_SCHEMA_VERSION,
            "workspace": self.workspace,
            "active_profile": self.active_profile,
            # 兼容旧版本字段，但不再把模型接入信息作为用户偏好持久化。
            "model_type": None,
            "model_name": None,
            "base_url": None,
            # 不将敏感凭据写入本地用户偏好文件，避免明文落盘。
            "api_key": None,
            "stream_output": self.stream_output,
            "confirm_dangerous": self.confirm_dangerous,
            "language": self.language,
            "prompt_style": normalize_prompt_style(self.prompt_style),
            "agent_mode": normalize_agent_mode(self.agent_mode),
            "show_startup_guide": self.show_startup_guide,
            "onboarding_completed": self.onboarding_completed,
            "last_used_at": self.last_used_at,
        }

    def to_display_dict(self) -> Dict[str, Any]:
        """转换为适合在终端展示的摘要。"""
        masked_key = None
        if self.api_key:
            if len(self.api_key) > 8:
                masked_key = f"{self.api_key[:4]}...{self.api_key[-4:]}"
            else:
                masked_key = "***"

        return {
            "workspace": self.workspace,
            "active_profile": self.active_profile,
            "model_type": self.model_type,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "api_key": masked_key,
            "stream_output": self.stream_output,
            "confirm_dangerous": self.confirm_dangerous,
            "language": self.language,
            "prompt_style": normalize_prompt_style(self.prompt_style),
            "agent_mode": normalize_agent_mode(self.agent_mode),
            "show_startup_guide": self.show_startup_guide,
            "onboarding_completed": self.onboarding_completed,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserConfig":
        """从字典创建用户配置。"""
        sanitized = {
            k: v for k, v in data.items()
            if k in cls.__dataclass_fields__
        }
        # 兼容旧配置文件，但不再信任或恢复已落盘的明文 API Key。
        sanitized["api_key"] = None
        sanitized["base_url"] = cls._sanitize_base_url(sanitized.get("base_url"))
        sanitized["language"] = normalize_language(sanitized.get("language"))
        sanitized["prompt_style"] = normalize_prompt_style(sanitized.get("prompt_style"))
        sanitized["agent_mode"] = normalize_agent_mode(sanitized.get("agent_mode"))
        return cls(**sanitized)

    def save(self, file_path: Optional[str] = None) -> Path:
        """保存到文件。"""
        path = Path(file_path) if file_path else self.default_path()
        self.last_used_at = datetime.now(timezone.utc).isoformat()
        write_private_json(path, self.to_dict())
        return path

    @classmethod
    def load(cls, file_path: Optional[str] = None) -> Optional["UserConfig"]:
        """从文件加载用户配置。"""
        path = Path(file_path) if file_path else cls.default_path()

        try:
            if not path.exists():
                return None

            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("schema_version") != SAYACODE_CONFIG_SCHEMA_VERSION:
                return None

            return cls.from_dict(data)
        except Exception:
            return None


# ==============================================================================
# 状态工厂
# ==============================================================================

def create_app_state(
    workspace: Path,
    model_type: str = "ollama",
    model_config: Optional[Dict[str, Any]] = None,
    max_history: int = 50,
    session_manager: Optional[SessionManager] = None,
    memory_manager: Optional[MemoryManager] = None,
    active_profile: Optional[str] = None,
    restored_session: bool = False,
    prompt_style: str = "standard",
    agent_mode: str = "build",
) -> AppState:
    """
    创建应用程序状态
    
    Args:
        workspace: 工作区路径
        model_type: 模型类型
        model_config: 模型配置
        max_history: 最大历史记录数
    
    Returns:
        AppState 实例
    """
    if model_config is None:
        model_config = {}
    
    # 创建会话管理器
    session = session_manager or SessionManager(max_messages=100)
    
    # 创建记忆管理器
    memory = memory_manager or MemoryManager(max_history=max_history)
    if not memory.interactions:
        memory.session_id = session.session_id
    
    # 创建安全检查器
    safety = SafetyChecker(
        auto_block_critical=True,
        allow_dangerous_commands=False,
        workspace_root=workspace
    )
    
    # 创建项目上下文
    context = ProjectContext(str(workspace))
    
    return AppState(
        workspace=workspace,
        model_type=model_type,
        model_config=model_config,
        session=session,
        memory=memory,
        safety=safety,
        context=context,
        active_profile=active_profile,
        restored_session=restored_session,
        prompt_style=prompt_style,
        agent_mode=agent_mode,
    )


def create_config_state(
    model_type: str = "ollama",
    **kwargs
) -> ConfigState:
    """
    创建配置状态
    
    Args:
        model_type: 默认模型类型
        **kwargs: 其他配置参数
    
    Returns:
        ConfigState 实例
    """
    return ConfigState(
        default_model_type=model_type,
        **kwargs
    )


def create_user_config(**kwargs) -> UserConfig:
    """创建用户偏好配置。"""
    return UserConfig(**kwargs)


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'AppState',
    'ConfigState',
    'UserConfig',
    'SAYACODE_CONFIG_SCHEMA_VERSION',
    'create_app_state',
    'create_config_state',
    'create_user_config',
]
