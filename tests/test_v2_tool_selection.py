"""Official model-driven tool selection in the actual agent graph."""

from __future__ import annotations

from pathlib import Path

from langchain.tools import tool
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from sayacode.config import Profile
from sayacode.runtime import AgentContext, AgentRuntime


class SelectingModel(BaseChatModel):
    selected: list[list[str]] = []
    selector_calls: int = 0
    selection: dict[str, list[str]] | None = {"tools": ["read_two"]}

    @property
    def _llm_type(self) -> str:
        return "selector-test"

    def with_structured_output(self, schema, **kwargs):
        self.selector_calls += 1
        return RunnableLambda(lambda _: self.selection)

    def bind_tools(self, tools, **kwargs):
        self.selected.append([item.name for item in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])


async def test_official_selector_limits_model_visible_tools(tmp_path: Path) -> None:
    @tool
    def read_one() -> str:
        """Read one source."""
        return "one"

    @tool
    def read_two() -> str:
        """Read two source."""
        return "two"

    context = AgentContext(
        workspace=tmp_path, mode="build", policy=None, output_dir=tmp_path,
        session_id="selector-thread",
    )
    model = SelectingModel()
    profile = Profile(
        name="test", protocol="openai_chat_completions", base_url="https://unused.test/v1",
        api_key="test-key", model_id="test", context_length=8192, max_output_tokens=512,
        file_search=False, summary_trigger_tokens=None,
        model_retries=0, tool_retries=0, tool_selector_max_tools=1,
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(
            profile, [read_one, read_two], context=context, model_override=model,
        )
        result = await runtime.invoke(handle, context, "Use the relevant reader")
    assert result.value["messages"][-1].content == "done"
    assert model.selector_calls == 1
    assert model.selected and model.selected[-1] == ["read_two"]


async def test_selector_uses_official_all_tools_fallback_for_unsupported_response(
    tmp_path: Path,
) -> None:
    @tool
    def read_one() -> str:
        """Read one source."""
        return "one"

    @tool
    def read_two() -> str:
        """Read two source."""
        return "two"

    context = AgentContext(
        workspace=tmp_path, mode="build", policy=None, output_dir=tmp_path,
        session_id="selector-fallback",
    )
    model = SelectingModel(selection=None)
    profile = Profile(
        name="test", protocol="openai_chat_completions", base_url="https://unused.test/v1",
        api_key="test-key", model_id="test", context_length=8192, max_output_tokens=512,
        file_search=False, summary_trigger_tokens=None,
        model_retries=0, tool_retries=0, tool_selector_max_tools=1,
    )
    async with await AgentRuntime.open(tmp_path / "state") as runtime:
        handle = runtime.build_agent(
            profile, [read_one, read_two], context=context, model_override=model,
        )
        result = await runtime.invoke(handle, context, "Use either reader")
    assert result.value["messages"][-1].content == "done"
    assert model.selector_calls == 1
    assert set(model.selected[-1]) == {"read_one", "read_two", "write_todos"}
