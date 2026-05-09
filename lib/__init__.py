"""Public runtime surface for SAYACODE."""

from ._version import __version__

from .agent import SAIAgent, create_sai_agent
from .api_config import APIConfig, APIConfigManager, APIConfigWizard, APIConfigWizardCLI, APIType
from .core.context import ChangeRecord, FileInfo, ProjectContext
from .core.memory import FileModification, Interaction, MemoryManager
from .core.safety import Operation, SafetyChecker, SafetyLevel, SafetyResult
from .core.session import Message, SessionManager
from .models import BaseModel
from .models.registry import ModelProviderRegistry, get_model_provider_registry
from .runtime import RuntimeContext
from .state import (
    AppState,
    ConfigState,
    UserConfig,
    create_app_state,
    create_config_state,
    create_user_config,
)
from .tools import ToolFactory, ToolRegistry, get_runtime_tool_catalog

__all__ = [
    "__version__",
    "APIType",
    "APIConfig",
    "APIConfigManager",
    "APIConfigWizard",
    "APIConfigWizardCLI",
    "SessionManager",
    "Message",
    "ProjectContext",
    "FileInfo",
    "ChangeRecord",
    "MemoryManager",
    "Interaction",
    "FileModification",
    "SafetyChecker",
    "SafetyLevel",
    "SafetyResult",
    "Operation",
    "BaseModel",
    "ModelProviderRegistry",
    "get_model_provider_registry",
    "RuntimeContext",
    "SAIAgent",
    "create_sai_agent",
    "AppState",
    "ConfigState",
    "UserConfig",
    "create_app_state",
    "create_config_state",
    "create_user_config",
    "ToolFactory",
    "ToolRegistry",
    "get_runtime_tool_catalog",
]
