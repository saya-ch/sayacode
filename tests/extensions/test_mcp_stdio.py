"""End-to-end MCP stdio server, workspace binding, trust, and approval."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths


class StdioCallModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "stdio-mcp-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        answer = (
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "mcp__working_directory",
                        "args": {},
                        "id": "stdio-call",
                        "type": "tool_call",
                    }
                ],
            )
            if self.calls == 1
            else AIMessage(content="MCP complete")
        )
        return ChatResult(generations=[ChatGeneration(message=answer)])

    def bind_tools(self, tools, **kwargs):
        return self


async def test_real_mcp_stdio_process_uses_trusted_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server_script = tmp_path / "server.py"
    server_script.write_text(
        "from fastmcp import FastMCP\n"
        "from pathlib import Path\n"
        "mcp = FastMCP('stdio-test')\n"
        "@mcp.tool\n"
        "def working_directory() -> dict[str, str]:\n"
        "    return {'cwd': str(Path.cwd().resolve())}\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test": {
                        "command": sys.executable,
                        "args": [str(server_script)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
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
    repository = ConfigRepository(paths.home)
    await repository.save(config)
    runtime = await AgentRuntime.open(paths.home)
    app = SayacodeApp(
        paths=paths,
        repository=repository,
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="mcp-stdio",
        trust_level="ask",
        profile_name="test",
        model_override=StdioCallModel(),
    )
    await app.initialize()
    try:
        assert app.mcp.tools == []
        app.config.trusted_mcp_projects = [str(workspace)]
        await app.repository.save(app.config)
        await app.mcp.reload()
        assert [item.name for item in app.mcp.tools] == ["mcp__working_directory"]
        paused = await app.run("Ask the MCP server for its working directory")
        assert paused["status"] == "paused"
        resumed = await app._resume_approval(
            "approve",
            {
                "thread_id": app.session_id,
                "decisions": [{"type": "approve"}],
                "grants": [],
            },
        )
        assert resumed["ok"] and resumed["response"] == "MCP complete"
        handle, _ = await app._context_for_thread(app.session_id)
        state = await app.runtime.get_state(handle, app.session_id)
        output = next(
            message
            for message in state.values["messages"]
            if isinstance(message, ToolMessage) and message.tool_call_id == "stdio-call"
        )
        assert output.artifact["structured_content"]["cwd"] == str(workspace.resolve())
        app.config.trusted_mcp_projects = []
        await app.repository.save(app.config)
        await app.mcp.reload()
        assert app.mcp.tools == []
    finally:
        await app.aclose()

