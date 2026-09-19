import sys
from types import ModuleType

import lib.runtime.interactive as interactive
from lib.commands import build_default_command_router
from lib.core.modes import apply_agent_mode_permissions
from lib.core.permission_workspace import create_permission_runtime
from lib.runtime import RuntimeContext
from lib.runtime.interactive import InteractiveLoop
from lib.runtime.state import create_app_state


class DummyAgent:
    tools = []
    model = None
    session = None

    def set_agent_mode(self, mode):
        self.mode = mode
        return mode


def test_readline_history_tolerates_missing_history_api(tmp_path, monkeypatch):
    readline = ModuleType("readline")
    monkeypatch.setitem(sys.modules, "readline", readline)
    monkeypatch.setattr(interactive, "_history_loaded", False)

    interactive._setup_readline_history(tmp_path / "history")

    assert interactive._history_loaded is True


def test_command_router_dispatches_core_command(tmp_path):
    runtime = RuntimeContext(
        workspace=tmp_path,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )
    router = build_default_command_router()

    result = router.dispatch("/help verbose", runtime)

    assert result is True


def test_command_router_returns_none_for_unknown_command(tmp_path):
    runtime = RuntimeContext(
        workspace=tmp_path,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )
    router = build_default_command_router()

    assert router.dispatch("/does-not-exist", runtime) is None


def test_quit_command_can_return_false(tmp_path):
    runtime = RuntimeContext(
        workspace=tmp_path,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )
    router = build_default_command_router()

    assert router.dispatch("/exit", runtime) is False


def test_mode_command_updates_state_and_runtime(tmp_path):
    state = create_app_state(
        workspace=tmp_path,
        model_type="ollama",
        model_config={"model_name": "unit"},
    )
    agent = DummyAgent()
    runtime = RuntimeContext.from_app_state(state, model_name="unit")
    runtime.permissions = create_permission_runtime(tmp_path)
    runtime.attach_agent(agent)
    state.runtime_context = runtime
    router = build_default_command_router()

    try:
        assert router.dispatch("/mode plan", runtime) is True

        assert state.agent_mode == "plan"
        assert runtime.agent_mode == "plan"
        # mode 规则写入 mode_rules，不再与 session 授权共用一个 dict。
        assert runtime.permissions.session.mode_rules["write_file"] == "deny"
        assert agent.mode == "plan"
    finally:
        apply_agent_mode_permissions("build", runtime=runtime.permissions)


def test_interactive_loop_dispatches_commands_through_runtime(tmp_path):
    state = create_app_state(
        workspace=tmp_path,
        model_type="ollama",
        model_config={"model_name": "unit"},
    )
    agent = DummyAgent()
    agent.session = state.session
    runtime = RuntimeContext.from_app_state(state, model_name="unit")
    runtime.permissions = create_permission_runtime(tmp_path)
    runtime.attach_agent(agent)
    state.runtime_context = runtime
    loop = InteractiveLoop(agent=agent, state=state, builtin_commands=("/mode",))

    try:
        assert loop.dispatch_command("/mode review") is True
        assert state.agent_mode == "review"
        assert runtime.agent_mode == "review"
        assert runtime.permissions.session.mode_rules["write_file"] == "deny"
        assert agent.mode == "review"
    finally:
        apply_agent_mode_permissions("build", runtime=runtime.permissions)


def test_interactive_loop_knows_custom_command_invocations(tmp_path):
    command_dir = tmp_path / ".claude" / "commands" / "ops"
    command_dir.mkdir(parents=True)
    (command_dir / "deploy.md").write_text("Deploy $ARGUMENTS", encoding="utf-8")
    state = create_app_state(
        workspace=tmp_path,
        model_type="ollama",
        model_config={"model_name": "unit"},
    )
    agent = DummyAgent()
    agent.session = state.session
    loop = InteractiveLoop(agent=agent, state=state, builtin_commands=("/help",))

    invocations = loop._known_command_invocations()

    assert "/help" in invocations
    assert "/deploy" in invocations
    assert "/ops:deploy" in invocations
