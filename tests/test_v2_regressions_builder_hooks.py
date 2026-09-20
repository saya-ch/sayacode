"""A trusted project hook executes in a builder's isolated Git worktree."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.app import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths
from sayacode.runtime import AgentRuntime


class BuilderModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "builder-hook-regression"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.calls == 0:
            message = AIMessage(content="", tool_calls=[{
                "name": "write_file",
                "args": {"path": "created.txt", "content": "builder output"},
                "id": "write-1",
            }])
        else:
            message = AIMessage(content="builder complete")
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.asyncio
async def test_builder_pre_tool_hook_runs_in_worktree_not_source(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "SAYACODE Test")
    _git(workspace, "config", "user.email", "sayacode-test@example.invalid")
    hook_config = workspace / ".sayacode" / "hooks.json"
    hook_config.parent.mkdir()
    hook_config.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "command": [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('hook-marker.txt').write_text('hook ran')",
        ],
        "blocking": True,
    }]}}), encoding="utf-8")
    (workspace / "README.md").write_text("temporary repository\n", encoding="utf-8")
    _git(workspace, "add", ".sayacode/hooks.json", "README.md")
    _git(workspace, "commit", "-m", "initial")

    paths = AppPaths.resolve(tmp_path / "state")
    repository = ConfigRepository(paths.home)
    config = Config(default_profile="test", profiles={
        "test": Profile(
            name="test", protocol="openai_chat_completions", base_url="https://unused.test/v1",
            api_key="test-key", model_id="test", context_length=8192, max_output_tokens=512,
            file_search=False,
            summary_trigger_tokens=None, model_retries=0, tool_retries=0,
            tool_selector_max_tools=None,
        )
    })
    await repository.save(config)
    runtime = await AgentRuntime.open(paths.home)
    app = await SayacodeApp(
        paths=paths, repository=repository, config=config, runtime=runtime,
        workspace=workspace, session_id="parent-session", mode="build",
        profile_name="test", model_override=BuilderModel(),
    ).initialize()
    try:
        app.hooks.trust()
        record = await app._spawn_task(
            "write created.txt", role="builder", parent_thread_id=app.session_id
        )
        settled = await app.wait_for_tasks()
        assert len(settled) == 1 and settled[0]["status"] == "completed"
        task_workspace = Path(record.task_workspace or "")
        assert task_workspace.is_dir()
        assert (task_workspace / "hook-marker.txt").read_text(encoding="utf-8") == "hook ran"
        assert (task_workspace / "created.txt").read_text(encoding="utf-8") == "builder output"
        assert not (workspace / "hook-marker.txt").exists()
        assert not (workspace / "created.txt").exists()
    finally:
        await app.aclose()
