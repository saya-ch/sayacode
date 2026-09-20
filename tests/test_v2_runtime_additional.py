"""Runtime API checks for model configuration and native event streaming."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.config import Profile
from sayacode.runtime import AgentContext, AgentHandle, AgentRuntime


class FixedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fixed"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def bind_tools(self, tools, **kwargs):
        return self


def test_profile_explicit_endpoint_and_key_win_over_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, dict[str, object]]] = []

    def capture(model: str, *, model_provider: str | None, **kwargs: object) -> object:
        calls.append((model, model_provider, kwargs))
        return object()

    monkeypatch.setattr("sayacode.runtime.init_chat_model", capture)
    profile = Profile(
        name="custom",
        model="chat",
        provider="openai",
        base_url="https://example.test/v1",
        api_key="key",
        config_fields={"temperature": 0, "base_url": "https://ignored.test"},
    )
    AgentRuntime._model_for(profile)
    assert calls == [
        (
            "chat",
            "openai",
            {
                "temperature": 0,
                "base_url": "https://example.test/v1",
                "api_key": "key",
            },
        )
    ]


def test_azure_profile_uses_azure_endpoint_and_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None, dict[str, object]]] = []

    def capture(model: str, *, model_provider: str | None, **kwargs: object) -> object:
        calls.append((model, model_provider, kwargs))
        return object()

    monkeypatch.setattr("sayacode.runtime.init_chat_model", capture)
    AgentRuntime._model_for(
        Profile(
            name="azure",
            model="deployment-a",
            provider="azure_openai",
            base_url="https://resource.openai.azure.com",
        )
    )
    assert calls == [
        (
            "deployment-a",
            "azure_openai",
            {
                "azure_endpoint": "https://resource.openai.azure.com",
                "azure_deployment": "deployment-a",
            },
        )
    ]


@pytest.mark.asyncio
async def test_v3_stream_finalizes_thread_status(tmp_path: Path) -> None:
    context = AgentContext(tmp_path, "build", None, tmp_path, "session-1")
    profile = Profile(
        name="fixed", model="fixed", file_search=False,
        summary_trigger_tokens=None, tool_selector_max_tools=None,
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(profile, [], context=context, model_override=FixedModel())
        run = await runtime.open_event_stream_v3(handle, context, "hello")
        async with run:
            async for _event in run:
                pass
            output = await run.output()
        assert output["messages"][-1].content == "ok"
        assert (await runtime.get_thread("session-1"))["status"] == "completed"


@pytest.mark.asyncio
async def test_manual_compact_rejects_pending_graph_state(tmp_path: Path) -> None:
    context = AgentContext(tmp_path, "build", None, tmp_path, "session-1")
    profile = Profile(name="fixed", model="fixed", tool_selector_max_tools=None)

    class PendingGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                next=("tools",),
                interrupts=(),
                values={"messages": [AIMessage(content="one"), AIMessage(content="two")]},
            )

    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = AgentHandle(PendingGraph(), profile, FixedModel(), tmp_path)
        with pytest.raises(RuntimeError, match="pending work"):
            await runtime.compact(handle, context)


@pytest.mark.asyncio
async def test_configured_context_window_triggers_official_auto_summary(tmp_path: Path) -> None:
    context = AgentContext(tmp_path, "build", None, tmp_path, "summary-thread")
    profile = Profile(
        name="small", model="fixed", file_search=False,
        config_fields={"profile": {"max_input_tokens": 1000}},
        summary_trigger_tokens=64_000, summary_keep_messages=2,
        tool_selector_max_tools=None,
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(profile, [], context=context, model_override=FixedModel())
        for index in range(4):
            await runtime.invoke(handle, context, f"turn {index} " + ("details " * 550))
        state = await runtime.get_state(handle, "summary-thread")
        assert len(state.values["messages"]) < 8
