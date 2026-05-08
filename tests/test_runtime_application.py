from lib.runtime import RuntimeApplication
from lib.runtime.startup import StartupOptions, StartupService
from lib.state import create_app_state


class DummyModel:
    model_name = "dummy"
    model_type = "dummy"
    context_window = 8192

    def chat(self, messages):
        return "ok"

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self


class DummyAgent:
    pass


def test_runtime_application_builds_context_and_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    state = create_app_state(
        workspace=tmp_path,
        model_type="ollama",
        model_config={"model_name": "unit", "context_window": 4096},
    )
    model = DummyModel()
    agent = DummyAgent()
    app = RuntimeApplication(api_manager="api", user_config="user", mcp_manager="mcp")

    context = app.build_context(state, model=model, model_name="unit", agent=agent)
    tools = app.build_tools(context)

    assert context.app_state is state
    assert context.model is model
    assert context.agent is agent
    assert context.config_stores["api"] == "api"
    assert context.config_stores["user"] == "user"
    assert context.config_stores["paths"].home == (tmp_path / "home").resolve()
    assert context.config_stores["config"].paths.home == (tmp_path / "home").resolve()
    assert context.config_stores["state"].paths.home == (tmp_path / "home").resolve()
    assert context.mcp == "mcp"
    assert context.permissions is not None
    assert context.permissions.workspace == tmp_path.resolve()
    assert context.hooks is not None
    assert context.hooks.workspace == tmp_path.resolve()
    assert context.prompt_style == state.prompt_style
    assert context.agent_mode == state.agent_mode
    assert context.tools == tools
    assert context.tool_registry is not None

    state.prompt_style = "concise"
    state.agent_mode = "plan"
    next_agent = DummyAgent()

    app.sync_state(context, state, agent=next_agent)

    assert context.prompt_style == "concise"
    assert context.agent_mode == "plan"
    assert context.agent is next_agent


def test_startup_service_bootstraps_runtime_context(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("lib.runtime.startup.create_runtime_model", lambda *args, **kwargs: DummyModel())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    service = StartupService(api_manager="api", user_config=None)
    result = service.bootstrap(StartupOptions(
        workspace=workspace,
        model_type="ollama",
        model_name="unit",
        model_config={"context_window": 8192},
        active_profile="unit-profile",
        prompt_style="standard",
        agent_mode="build",
        stream_output=False,
        confirm_dangerous=True,
    ))

    assert result.runtime.workspace == workspace.resolve()
    assert result.runtime.model is result.model
    assert result.runtime.agent is result.agent
    assert result.runtime.permissions is not None
    assert result.runtime.permissions.workspace == workspace.resolve()
    assert result.runtime.hooks is not None
    assert result.runtime.hooks.workspace == workspace.resolve()
    assert result.agent._mcp_runtime.permissions is result.runtime.permissions
    assert result.agent._mcp_runtime.hooks is result.runtime.hooks
    assert result.state.runtime_context is result.runtime
    assert result.state.session.model_context_limit == 8192
    assert result.runtime.tools
    assert result.mcp.get_server_count() == 0
