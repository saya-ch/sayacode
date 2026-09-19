"""SAYACODE 启动装配的 runtime 应用服务。

构建并同步 RuntimeContext 与工具注册表，核心类为 RuntimeApplication，
供 StartupService 在 bootstrap 阶段装配调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..core.hooks import create_hook_runtime
from ..core.paths import ConfigStore, SayacodePaths, StateStore
from lib.core.permission_workspace import create_permission_runtime
from ..tools import ToolRegistry
from .context import RuntimeContext


@dataclass
class RuntimeApplication:
    """构建并同步 runtime context 对象。"""

    api_manager: Optional[Any] = None
    user_config: Optional[Any] = None
    mcp_manager: Optional[Any] = None

    def _config_stores(self) -> dict[str, Any]:
        paths = SayacodePaths.resolve(create=True)
        return {
            "api": self.api_manager,
            "user": self.user_config,
            "paths": paths,
            "config": ConfigStore(paths),
            "state": StateStore(paths),
        }

    def build_context(
        self,
        state: Any,
        *,
        model: Optional[Any] = None,
        model_name: Optional[str] = None,
        agent: Optional[Any] = None,
    ) -> RuntimeContext:
        context = RuntimeContext.from_app_state(
            state,
            model_name=model_name,
            mcp=self.mcp_manager,
            config_stores=self._config_stores(),
        )
        context.model = model
        context.permissions = create_permission_runtime(context.workspace)
        context.hooks = create_hook_runtime(context.workspace)
        if agent is not None:
            context.attach_agent(agent)
        return context

    def build_tools(self, context: RuntimeContext) -> list[Any]:
        from ..agent.assembly import build_supervisor_factory

        registry = ToolRegistry(context, supervisor_factory=build_supervisor_factory(context))
        tools = registry.build_tools()
        context.attach_tools(tools, registry=registry)
        return tools

    def attach_agent(self, context: RuntimeContext, agent: Any) -> None:
        context.attach_agent(agent)
        agent_tools = getattr(agent, "tools", None)
        if isinstance(agent_tools, list):
            context.attach_tools(agent_tools, registry=context.tool_registry)

    def sync_state(
        self,
        context: RuntimeContext,
        state: Any,
        *,
        model: Optional[Any] = None,
        model_name: Optional[str] = None,
        agent: Optional[Any] = None,
    ) -> RuntimeContext:
        context.sync_from_app_state(state, model_name=model_name)
        if model is not None:
            context.model = model
        if agent is not None:
            context.attach_agent(agent)
        context.mcp = self.mcp_manager
        if context.permissions is not None and hasattr(context.permissions, "configure_workspace"):
            context.permissions.configure_workspace(context.workspace)
        else:
            context.permissions = create_permission_runtime(context.workspace)
        if context.hooks is not None and hasattr(context.hooks, "configure_workspace"):
            context.hooks.configure_workspace(context.workspace)
        else:
            context.hooks = create_hook_runtime(context.workspace)
        context.config_stores.update(self._config_stores())
        return context


__all__ = ["RuntimeApplication"]
