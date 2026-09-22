"""MCP text output stays locatable without filling the graph checkpoint."""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP
from langchain.mcp import MCPAdapter
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.extensions.mcp import namespace_mcp_tools
from sayacode.paths import AppPaths


class ToolThenDone(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "large-mcp-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        response = (
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "mcp__large", "args": {}, "id": "large-call", "type": "tool_call"}
                ],
            )
            if self.calls == 1
            else AIMessage(content="done")
        )
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):
        return self


async def test_large_mcp_text_spills_without_changing_tool_status(tmp_path: Path) -> None:
    server = FastMCP("large-output")
    content = "汉字-data-" * 15_000

    @server.tool
    async def large() -> str:
        return content

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = AppPaths.resolve(tmp_path / "state")
    profile = Profile(
        name="test",
        protocol="openai_chat_completions",
        base_url="https://unused.test/v1",
        api_key="test-key",
        model_id="test",
        context_length=8192,
        max_output_tokens=512,
        file_search=False,
        summary_trigger_tokens=None,
        summary_trigger_ratio=None,
        tool_selector_max_tools=None,
        model_retries=0,
        tool_retries=0,
    )
    config = Config(default_profile="test", profiles={"test": profile})
    repo = ConfigRepository(paths.home)
    await repo.save(config)
    runtime = await AgentRuntime.open(paths.home)
    app = SayacodeApp(
        paths=paths,
        repository=repo,
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="large-mcp",
        trust_level="ask",
        profile_name="test",
        model_override=ToolThenDone(),
    )
    async with MCPAdapter(server) as adapter:
        await app.initialize()
        app.mcp.tools = namespace_mcp_tools(await adapter.list_tools())
        try:
            pending = await app.run("Call the large remote tool")
            assert pending["status"] == "paused"
            result = await app.command(
                "approve",
                {
                    "thread_id": "large-mcp",
                    "decisions": [{"type": "approve"}],
                    "grants": [],
                },
            )
            assert result["ok"] and result["response"] == "done"
            handle, _ = await app._context_for_thread("large-mcp")
            state = await app.runtime.get_state(handle, "large-mcp")
            output = next(
                message
                for message in state.values["messages"]
                if isinstance(message, ToolMessage) and message.tool_call_id == "large-call"
            )
            assert output.status == "success"
            assert len(str(output.content).encode("utf-8")) < len(content.encode("utf-8"))
            assert "Full MCP output" in str(output.content)
            assert len(str(output.artifact).encode("utf-8")) < len(content.encode("utf-8"))
            files = list(paths.outputs.glob("mcp-*.txt"))
            assert len(files) == 1 and files[0].read_text(encoding="utf-8") == content
        finally:
            await app.aclose()

