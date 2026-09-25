"""Focused checks of the new framework-owned runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.agent import AgentContext, AgentRuntime
from sayacode.config import Config, ConfigRepository, Profile, normalize_trust


class FixedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fixed-test-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_config_repository_round_trip(tmp_path: Path) -> None:
    repo = ConfigRepository(tmp_path)
    assert await repo.load() == Config()
    config = Config(
        default_profile="test",
        profiles={
            "test": Profile(
                name="test",
                protocol="openai_chat_completions",
                base_url="https://unused.test/v1",
                api_key="test-key",
                model_id="fixed",
                context_length=8192,
                max_output_tokens=512,
            )
        },
    )
    await repo.save(config)
    loaded = await repo.load()
    assert loaded.profile().name == "test"
    assert loaded.profile().protocol == "openai_chat_completions"


@pytest.mark.asyncio
async def test_old_reviewer_configuration_is_removed_on_load(tmp_path: Path) -> None:
    repo = ConfigRepository(tmp_path)
    saved = Config().to_dict()
    saved["default_trust"] = "jev"
    saved["jev"] = {"api_key": "legacy-secret"}
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    repo.path.write_text(json.dumps(saved), encoding="utf-8")

    loaded = await repo.load()
    persisted = json.loads(repo.path.read_text(encoding="utf-8"))
    assert loaded.default_trust == "ask"
    assert persisted["default_trust"] == "ask"
    assert "jev" not in persisted
    assert "legacy-secret" not in repo.path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown trust level"):
        normalize_trust("jev")


@pytest.mark.asyncio
async def test_graph_checkpoint_store_compact_and_rewind(tmp_path: Path) -> None:
    context = AgentContext(
        workspace=tmp_path,
        trust_level="ask",
        policy=None,
        output_dir=tmp_path,
        session_id="session-1",
        profile_name="test",
    )
    profile = Profile(
        name="test",
        protocol="openai_chat_completions",
        base_url="https://unused.test/v1",
        api_key="test-key",
        model_id="fixed",
        context_length=8192,
        max_output_tokens=512,
        file_search=False,
        summary_trigger_tokens=None,
        summary_trigger_ratio=None,
        tool_selector_max_tools=None,
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(profile, [], context=context, model_override=FixedModel())
        for index in range(3):
            result = await runtime.invoke(handle, context, f"turn {index}")
            assert result.value["messages"][-1].content == "done"
        assert (await runtime.get_thread("session-1"))["status"] == "completed"
        assert len(await runtime.list_threads(workspace=tmp_path)) == 1
        previous = await runtime.get_state(handle, "session-1")
        assert await runtime.compact(handle, context, focus="parser {edge cases}")
        current = await runtime.get_state(handle, "session-1")
        assert len(current.values["messages"]) < len(previous.values["messages"])

        history = await runtime.get_history(handle, "session-1")
        checkpoint_id = history[-3].config["configurable"]["checkpoint_id"]
        fork = await runtime.rewind(handle, "session-1", checkpoint_id)
        assert fork["configurable"]["checkpoint_id"] != checkpoint_id
        assert (await runtime.get_thread("session-1"))["status"] == "rewound"

    assert (tmp_path / "state" / "checkpoints.sqlite3").exists()
    assert (tmp_path / "state" / "store.sqlite3").exists()

