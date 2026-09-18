"""SAYACODE 的公开运行时接口。

本包 ``__init__`` 刻意保持轻量：只导版本号，其余全部惰性加载。
之前这里 eager import 了 agent/models/tools 全家桶，导致 ``import lib.theme``
（只需要 ``lib.i18n``）也要付约 17 秒的 import 代价——实测 ``lib/__init__``
执行即拖入整个 agent 栈。按 PEP 562 用 ``__getattr__`` 延迟到首次使用。
"""

from ._version import __version__

_LAZY_EXPORTS = {
    "SAIAgent": (".agent", "SAIAgent"),
    "create_sai_agent": (".agent", "create_sai_agent"),
    "APIConfig": (".api_config", "APIConfig"),
    "APIConfigManager": (".api_config", "APIConfigManager"),
    "APIConfigWizard": (".api_config", "APIConfigWizard"),
    "APIConfigWizardCLI": (".api_config", "APIConfigWizardCLI"),
    "APIType": (".api_config", "APIType"),
    "ProjectContext": (".core.context", "ProjectContext"),
    "FileInfo": (".core.context", "FileInfo"),
    "ChangeRecord": (".core.context", "ChangeRecord"),
    "SessionDerivedMemoryView": (".core.session", "SessionDerivedMemoryView"),
    "load_legacy_memory_json": (".core.session", "load_legacy_memory_json"),
    "SafetyChecker": (".core.safety", "SafetyChecker"),
    "SafetyLevel": (".core.safety", "SafetyLevel"),
    "SafetyResult": (".core.safety", "SafetyResult"),
    "Operation": (".core.safety", "Operation"),
    "SessionManager": (".core.session", "SessionManager"),
    "Message": (".core.session", "Message"),
    "BaseModel": (".models", "BaseModel"),
    "ModelProviderRegistry": (".models.registry", "ModelProviderRegistry"),
    "get_model_provider_registry": (".models.registry", "get_model_provider_registry"),
    "RuntimeContext": (".runtime", "RuntimeContext"),
    "AppState": (".state", "AppState"),
    "ConfigState": (".state", "ConfigState"),
    "UserConfig": (".state", "UserConfig"),
    "create_app_state": (".state", "create_app_state"),
    "create_config_state": (".state", "create_config_state"),
    "create_user_config": (".state", "create_user_config"),
    "ToolFactory": (".tools", "ToolFactory"),
    "ToolRegistry": (".tools", "ToolRegistry"),
    "get_runtime_tool_catalog": (".tools", "get_runtime_tool_catalog"),
}


def __getattr__(name: str):
    """PEP 562 惰性导出：首次访问时才 import 对应子模块。"""
    if name in _LAZY_EXPORTS:
        import importlib

        module_name, attr_name = _LAZY_EXPORTS[name]
        module = importlib.import_module(module_name, __name__)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "SessionDerivedMemoryView",
    "load_legacy_memory_json",
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
