"""检查运行时接口，覆盖模型配置和原生事件流。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.agent import AgentContext, AgentHandle, AgentRuntime
from sayacode.agent.models import model_for
from sayacode.config import Profile


class FixedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fixed"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def bind_tools(self, tools, **kwargs):
        return self


def test_chat_completions_protocol_passes_explicit_endpoint_key_and_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, dict[str, object]]] = []

    def capture(model: str, *, model_provider: str | None, **kwargs: object) -> object:
        calls.append((model, model_provider, kwargs))
        return object()

    monkeypatch.setattr("sayacode.agent.models.init_chat_model", capture)
    profile = Profile(
        name="custom",
        protocol="openai_chat_completions",
        base_url="https://example.test/v1",
        api_key="key",
        model_id="chat",
        context_length=8192,
        max_output_tokens=512,
    )
    model_for(profile)
    assert calls == [
        (
            "chat",
            "openai",
            {
                "base_url": "https://example.test/v1",
                "api_key": "key",
                "use_responses_api": False,
                "max_tokens": 512,
            },
        )
    ]


def test_responses_protocol_explicitly_selects_responses_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, dict[str, object]]] = []

    def capture(model: str, *, model_provider: str | None, **kwargs: object) -> object:
        calls.append((model, model_provider, kwargs))
        return object()

    monkeypatch.setattr("sayacode.agent.models.init_chat_model", capture)
    model_for(
        Profile(
            name="responses",
            protocol="openai_responses",
            base_url="https://example.test/v1",
            api_key="key",
            model_id="coder",
            context_length=8192,
            max_output_tokens=1024,
        )
    )
    assert calls == [
        (
            "coder",
            "openai",
            {
                "base_url": "https://example.test/v1",
                "api_key": "key",
                "use_responses_api": True,
                "max_tokens": 1024,
            },
        )
    ]


@pytest.mark.asyncio
async def test_v3_stream_finalizes_thread_status(tmp_path: Path) -> None:
    context = AgentContext(tmp_path, "build", None, tmp_path, "session-1")
    profile = Profile(
        name="fixed",
        protocol="openai_chat_completions",
        base_url="https://unused.test/v1",
        api_key="test-key",
        model_id="fixed",
        context_length=8192,
        max_output_tokens=512,
        file_search=False,
        summary_trigger_tokens=None,
        tool_selector_max_tools=None,
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
    profile = Profile(
        name="fixed",
        protocol="openai_chat_completions",
        base_url="https://unused.test/v1",
        api_key="test-key",
        model_id="fixed",
        context_length=8192,
        max_output_tokens=512,
        tool_selector_max_tools=None,
    )

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
        name="small",
        protocol="openai_chat_completions",
        base_url="https://unused.test/v1",
        api_key="test-key",
        model_id="fixed",
        context_length=1000,
        max_output_tokens=128,
        file_search=False,
        summary_trigger_tokens=64_000,
        summary_keep_messages=2,
        tool_selector_max_tools=None,
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(profile, [], context=context, model_override=FixedModel())
        for index in range(4):
            await runtime.invoke(handle, context, f"turn {index} " + ("details " * 550))
        state = await runtime.get_state(handle, "summary-thread")
        assert len(state.values["messages"]) < 8
