"""Runtime startup services for SAYACODE."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..agent import SAIAgent
from ..core.modes import apply_agent_mode_permissions
from ..custom_commands import load_project_mcp_config
from ..state import UserConfig, create_app_state
from .app import RuntimeApplication
from .context import RuntimeContext
from .model_profiles import create_runtime_model
from .session_store import (
    load_runtime_managers,
    persist_local_state,
    sync_session_model_runtime,
)


@dataclass
class ProjectMCPService:
    """Workspace MCP config adapter owned by one runtime startup."""

    workspace: Path
    runtime: Optional[RuntimeContext] = None
    servers: list[str] = None

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        self.servers = list(self.servers or [])
        self.config_path = self.workspace / ".mcp.json"

    def load_config(self) -> bool:
        """Load configured project MCP server names without starting them."""
        _, config = load_project_mcp_config(self.workspace)
        servers = config.get("mcpServers", {}) if isinstance(config, dict) else {}
        self.servers = list(servers) if isinstance(servers, dict) else []
        return bool(self.servers)

    def attach_runtime(self, runtime: RuntimeContext) -> None:
        self.runtime = runtime

    def get_server_count(self) -> int:
        agent = getattr(self.runtime, "agent", None) if self.runtime is not None else None
        if agent is not None and hasattr(agent, "get_mcp_registry"):
            status = agent.get_mcp_registry()
            if isinstance(status, dict):
                active = status.get("active_servers", {})
                if isinstance(active, dict) and active:
                    return len(active)
        return len(self.servers)

    def list_servers(self) -> list[str]:
        return list(self.servers)


@dataclass(frozen=True)
class StartupOptions:
    """Resolved startup inputs for one SAYACODE runtime."""

    workspace: Path
    model_type: str
    model_name: str
    model_config: Dict[str, Any]
    active_profile: Optional[str]
    prompt_style: str
    agent_mode: str
    stream_output: bool
    confirm_dangerous: bool
    requested_session_id: Optional[str] = None
    create_new_session: bool = False


@dataclass
class StartupResult:
    """Objects created by runtime startup."""

    app: RuntimeApplication
    runtime: RuntimeContext
    state: Any
    agent: SAIAgent
    model: Any
    mcp: ProjectMCPService


@dataclass
class StartupService:
    """Create the runtime context, tools, model, session, and agent."""

    api_manager: Optional[Any] = None
    user_config: Optional[UserConfig] = None

    def bootstrap(self, options: StartupOptions) -> StartupResult:
        workspace = Path(options.workspace).expanduser().resolve()
        mcp_service = ProjectMCPService(workspace)
        mcp_service.load_config()

        model = create_runtime_model(
            options.model_type,
            model_name=options.model_name,
            **dict(options.model_config or {}),
        )

        session_manager, memory_manager, restored_session = load_runtime_managers(
            workspace,
            requested_session_id=options.requested_session_id,
            create_new=options.create_new_session,
        )
        sync_session_model_runtime(session_manager, model)

        state = create_app_state(
            workspace=workspace,
            model_type=options.model_type,
            model_config={"model_name": options.model_name, **dict(options.model_config or {})},
            session_manager=session_manager,
            memory_manager=memory_manager,
            active_profile=options.active_profile,
            restored_session=restored_session,
            prompt_style=options.prompt_style,
            agent_mode=options.agent_mode,
        )
        state.stream_output = options.stream_output
        state.confirm_dangerous = options.confirm_dangerous
        apply_agent_mode_permissions(state.agent_mode)

        app = RuntimeApplication(
            api_manager=self.api_manager,
            user_config=self.user_config,
            mcp_manager=mcp_service,
        )
        runtime = app.build_context(state, model=model, model_name=options.model_name)
        state.runtime_context = runtime
        runtime_tools = app.build_tools(runtime)
        mcp_service.attach_runtime(runtime)

        persist_local_state(state, self.user_config)

        agent = SAIAgent(
            model=model,
            workspace=workspace,
            tools=runtime_tools,
            memory_manager=state.memory,
            safety_checker=state.safety,
            project_context=state.context,
            session_manager=state.session,
            prompt_style=state.prompt_style,
            agent_mode=state.agent_mode,
            enable_mcp=True,
            permissions=runtime.permissions,
            hooks=runtime.hooks,
        )
        app.attach_agent(runtime, agent)

        return StartupResult(
            app=app,
            runtime=runtime,
            state=state,
            agent=agent,
            model=model,
            mcp=mcp_service,
        )


__all__ = [
    "ProjectMCPService",
    "StartupOptions",
    "StartupResult",
    "StartupService",
]
