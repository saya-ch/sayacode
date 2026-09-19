from lib.runtime import RuntimeApplication
from lib.runtime.startup import ProjectMCPService, StartupOptions, StartupService
from lib.runtime.state import create_app_state
from lib.core.tool_meta import ToolMeta, register_tool_meta
from langchain_core.tools import StructuredTool


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


def test_project_mcp_service_default_server_lists_are_independent(tmp_path):
    first = ProjectMCPService(tmp_path / "one")
    second = ProjectMCPService(tmp_path / "two")

    first.servers.append("demo")

    assert first.list_servers() == ["demo"]
    assert second.list_servers() == []


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
    tool_names = {tool.name for tool in tools}

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
    assert {"ToolSearch", "invoke_tool", "batch_execute"} <= tool_names
    assert "get_project_summary" not in tool_names

    state.prompt_style = "concise"
    state.agent_mode = "plan"
    next_agent = DummyAgent()

    app.sync_state(context, state, agent=next_agent)

    assert context.prompt_style == "concise"
    assert context.agent_mode == "plan"
    assert context.agent is next_agent


def test_registry_defers_external_tools_into_unified_orchestration(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    state = create_app_state(
        workspace=tmp_path,
        model_type="ollama",
        model_config={"model_name": "unit", "context_window": 4096},
    )
    app = RuntimeApplication()
    context = app.build_context(state, model=DummyModel(), model_name="unit")
    app.build_tools(context)
    external = StructuredTool.from_function(
        func=lambda message: f"external:{message}",
        name="mcp_demo_echo",
        description="Echo through a demo MCP server.",
    )
    register_tool_meta(ToolMeta.safe_default(
        "mcp_demo_echo",
        description="stale eager metadata",
        should_defer=False,
        always_load=True,
    ))

    tools = context.tool_registry.compose_tools([external])
    tool_map = {tool.name: tool for tool in tools}

    assert "mcp_demo_echo" not in tool_map
    search_result = tool_map["ToolSearch"].invoke({"query": "mcp_demo_echo"})
    assert "mcp_demo_echo" in search_result
    assert "参数 schema" in search_result
    assert tool_map["invoke_tool"].invoke({
        "tool_name": "mcp_demo_echo",
        "arguments": {"message": "ok"},
    }) == "external:ok"


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
