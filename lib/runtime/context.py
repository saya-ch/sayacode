"""Explicit runtime context container for SAYACODE."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.context import ProjectContext
from ..core.memory import MemoryManager
from ..core.safety import SafetyChecker
from ..core.session import SessionManager


@dataclass
class RuntimeContext:
    """Stable container for runtime-scoped services.

    The CLI still owns startup and terminal I/O, but tools and runners should
    receive this context instead of reading process-wide workspace/model state.
    """

    workspace: Path
    model_type: str
    model_name: str
    model_config: Dict[str, Any] = field(default_factory=dict)
    model: Optional[Any] = None
    prompt_style: str = "standard"
    agent_mode: str = "build"
    session: Optional[SessionManager] = None
    memory: Optional[MemoryManager] = None
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
        """Build a runtime context from the current CLI AppState."""
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
        """Refresh runtime-scoped state from AppState after a switch."""
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
        """Attach the active agent facade."""
        self.agent = agent

    def attach_tools(self, tools: list[Any], registry: Optional[Any] = None) -> None:
        """Attach runtime-bound tools and the registry that built them."""
        self.tools = list(tools or [])
        if registry is not None:
            self.tool_registry = registry

    def resolve_workspace_path(self, path: str | Path) -> Path:
        """Resolve a path inside this runtime workspace."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()

    @property
    def context_window(self) -> int:
        """Return the configured model context window, or 0 when unknown."""
        value = self.model_config.get("context_window")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
