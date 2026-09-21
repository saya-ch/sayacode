"""Real graph checks for model failure and provider-declared truncation."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.agent import AgentContext, AgentRuntime
from sayacode.agent.events import _final_text
from sayacode.config import Profile


class SequenceModel(BaseChatModel):
    responses: list[AIMessage] = []
    calls: int = 0
    fail: bool = False

    @property
    def _llm_type(self) -> str:
        return "sequence-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.fail:
            raise TimeoutError("provider timeout")
        answer = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return ChatResult(generations=[ChatGeneration(message=answer)])

    def bind_tools(self, tools, **kwargs):
        return self


def context(root: Path) -> AgentContext:
    return AgentContext(
        workspace=root,
        trust_level="ask",
        policy=None,
        output_dir=root,
        session_id="model-test",
    )


def profile(*, retries: int = 0) -> Profile:
    return Profile(
        name="test",
        protocol="openai_chat_completions",
        base_url="https://unused.test/v1",
        api_key="test-key",
        model_id="test",
        context_length=8192,
        max_output_tokens=512,
        file_search=False,
        summary_trigger_tokens=None,
        model_retries=retries,
        tool_retries=0,
        tool_selector_max_tools=None,
    )


async def test_model_retry_exhaustion_is_a_failed_graph_run(tmp_path: Path) -> None:
    model = SequenceModel(fail=True)
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(
            profile(retries=1), [], context=context(tmp_path), model_override=model
        )
        with pytest.raises(TimeoutError):
            await runtime.invoke(handle, context(tmp_path), "hello")
        assert model.calls == 2
        assert (await runtime.get_thread("model-test"))["status"] == "error"


async def test_explicit_truncation_continues_within_model_budget(tmp_path: Path) -> None:
    model = SequenceModel(
        responses=[
            AIMessage(content="first ", response_metadata={"finish_reason": "length"}),
            AIMessage(content="second", response_metadata={"finish_reason": "stop"}),
        ]
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(profile(), [], context=context(tmp_path), model_override=model)
        result = await runtime.invoke(handle, context(tmp_path), "respond")
        assert model.calls == 2
        assert _final_text(result) == "first second"
        assert (await runtime.get_thread("model-test"))["status"] == "completed"


async def test_truncation_continues_without_product_model_budget(tmp_path: Path) -> None:
    model = SequenceModel(
        responses=[
            AIMessage(content="part one ", response_metadata={"stop_reason": "max_tokens"}),
            AIMessage(content="part two ", response_metadata={"stop_reason": "max_tokens"}),
            AIMessage(content="complete", response_metadata={"stop_reason": "stop"}),
        ]
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(profile(), [], context=context(tmp_path), model_override=model)
        result = await runtime.invoke(handle, context(tmp_path), "respond")
        assert model.calls == 3
        assert _final_text(result) == "part one part two complete"
        assert (await runtime.get_thread("model-test"))["status"] == "completed"
