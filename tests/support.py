"""集成测试共享的真实应用与脚本模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths


class ContractModel(BaseChatModel):
    responses: list[AIMessage] = [AIMessage(content="done")]
    calls: int = 0
    received: list[list[Any]] = []

    @property
    def _llm_type(self) -> str:
        return "contract-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(list(messages))
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):
        return self


async def contract_app(
    tmp_path: Path, model: BaseChatModel | None = None, *, session_id="contract-session"
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    paths = AppPaths.resolve(tmp_path / "state")
    repository = ConfigRepository(paths.home)
    if repository.path.exists():
        config = await repository.load()
    else:
        config = Config(
            default_profile="test",
            profiles={
                "test": Profile(
                    name="test",
                    protocol="openai_chat_completions",
                    base_url="https://unused.test/v1",
                    api_key="test-key",
                    model_id="test",
                    context_length=8192,
                    max_output_tokens=512,
                    file_search=False,
                    summary_trigger_tokens=None,
                    tool_selector_max_tools=None,
                    model_retries=0,
                    tool_retries=0,
                ),
            },
        )
        await repository.save(config)
    runtime = await AgentRuntime.open(paths.home)
    app = SayacodeApp(
        paths=paths,
        repository=repository,
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id=session_id,
        trust_level="ask",
        profile_name=config.default_profile,
        model_override=model or ContractModel(),
    )
    return await app.initialize()
