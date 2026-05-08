from lib.api_config import APIConfig, APIConfigManager, APIType
from lib.runtime import RuntimeContext
from lib.runtime.model_profiles import switch_active_profile
from lib.state import create_app_state


class DummyAgent:
    tools = []

    def __init__(self):
        self.rebuilds = 0

    def _create_agent(self):
        self.rebuilds += 1


def test_switch_active_profile_syncs_state_runtime_and_session(tmp_path):
    manager = APIConfigManager(config_dir=str(tmp_path / "config"))
    manager.add_config(
        "local",
        APIConfig(
            api_type=APIType.OLLAMA,
            base_url="http://localhost:11434",
            model_name="unit-model",
            context_window="64k",
        ),
    )
    manager.set_current("local")
    state = create_app_state(
        workspace=tmp_path / "workspace",
        model_type="ollama",
        model_config={"model_name": "old-model", "context_window": 4096},
    )
    agent = DummyAgent()
    runtime = RuntimeContext.from_app_state(state, model_name="old-model")
    runtime.attach_agent(agent)
    state.runtime_context = runtime

    result = switch_active_profile(agent, state, api_manager=manager)

    assert result.ok is True
    assert result.changed is True
    assert state.active_profile == "local"
    assert state.model_config["model_name"] == "unit-model"
    assert state.session.model_context_limit == 65536
    assert runtime.model_name == "unit-model"
    assert runtime.model is agent.model
    assert agent.rebuilds == 1
