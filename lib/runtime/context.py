"""SAYACODE 的显式 runtime context 容器。

承载 workspace、模型、会话与工具等作用域服务，核心类为 RuntimeContext，
供 tools 与 runners 接收并替代进程级全局状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.context import ProjectContext
from ..core.safety import SafetyChecker
from ..core.session import SessionManager


@dataclass
class RuntimeContext:
    """runtime 作用域服务的稳定容器。

    CLI 仍然负责启动与终端 I/O，但 tools 和 runners 应接收此 context，
    而不是读取进程级的 workspace/model 状态。
    """

    workspace: Path
    model_type: str
    model_name: str
    model_config: Dict[str, Any] = field(default_factory=dict)
    model: Optional[Any] = None
    prompt_style: str = "standard"
    agent_mode: str = "build"
    session: Optional[SessionManager] = None
    memory: Optional[Any] = None
    safety: Optional[SafetyChecker] = None
    project_context: Optional[ProjectContext] = None
    app_state: Optional[Any] = None
    agent: Optional[Any] = None
    tools: list[Any] = field(default_factory=list)
    permissions: Optional[Any] = None
    hooks: Optional[Any] = None
    mcp: Optional[Any] = None
    tool_registry: Optional[Any] = None
    config_stores: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        self.model_config = dict(self.model_config or {})

    @classmethod
    def from_app_state(
        cls,
        state: Any,
        *,
        model_name: Optional[str] = None,
        mcp: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        config_stores: Optional[Dict[str, Any]] = None,
    ) -> "RuntimeContext":
        """从当前 CLI AppState 构建 runtime context。"""
        state_model_config = dict(getattr(state, "model_config", {}) or {})
        resolved_model_name = model_name or state_model_config.get("model_name") or ""
        return cls(
            workspace=getattr(state, "workspace"),
            model_type=getattr(state, "model_type"),
            model_name=resolved_model_name,
            model_config=state_model_config,
            session=getattr(state, "session", None),
            memory=getattr(state, "memory", None),
            safety=getattr(state, "safety", None),
            project_context=getattr(state, "context", None),
            prompt_style=getattr(state, "prompt_style", "standard"),
            agent_mode=getattr(state, "agent_mode", "build"),
            app_state=state,
            mcp=mcp,
            tool_registry=tool_registry,
            config_stores=dict(config_stores or {}),
        )

    def sync_from_app_state(self, state: Any, *, model_name: Optional[str] = None) -> None:
        """切换后从 AppState 刷新 runtime 作用域状态。"""
        state_model_config = dict(getattr(state, "model_config", {}) or {})
        self.workspace = Path(getattr(state, "workspace")).expanduser().resolve()
        self.model_type = getattr(state, "model_type")
        self.model_name = model_name or state_model_config.get("model_name") or self.model_name
        self.model_config = state_model_config
        self.prompt_style = getattr(state, "prompt_style", self.prompt_style)
        self.agent_mode = getattr(state, "agent_mode", self.agent_mode)
        self.session = getattr(state, "session", None)
        self.memory = getattr(state, "memory", None)
        self.safety = getattr(state, "safety", None)
        self.project_context = getattr(state, "context", None)
        self.app_state = state
        if self.permissions is not None and hasattr(self.permissions, "configure_workspace"):
            self.permissions.configure_workspace(self.workspace)
        if self.hooks is not None and hasattr(self.hooks, "configure_workspace"):
            self.hooks.configure_workspace(self.workspace)

    def attach_agent(self, agent: Any) -> None:
        """挂载当前活跃的 agent 门面。"""
        self.agent = agent

    def attach_tools(self, tools: list[Any], registry: Optional[Any] = None) -> None:
        """挂载绑定到 runtime 的 tools 以及构建它们的 registry。"""
        self.tools = list(tools or [])
        if registry is not None:
            self.tool_registry = registry

    def resolve_workspace_path(self, path: str | Path) -> Path:
        """解析此 runtime workspace 内的路径。"""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()

    @property
    def context_window(self) -> int:
        """返回已配置的模型 context window，未知时返回 0。"""
        value = self.model_config.get("context_window")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
