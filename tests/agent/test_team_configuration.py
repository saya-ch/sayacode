"""单 Agent 对照只改变模型实际可调用的团队工具，不改变执行链路。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths
from tests.support import ContractModel

TEAM_TOOLS = {
    "delegate_to_subagent",
    "send_message_to_subagent",
    "task_status",
    "task_delivery",
    "task_wait",
}


class ToolVisibilityModel(ContractModel):
    bound_tool_names: list[set[str]] = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names.append({tool.name for tool in tools})
        return self


async def make_app(
    tmp_path: Path,
    model: ToolVisibilityModel,
    *,
    team_enabled: bool | None = None,
) -> SayacodeApp:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = AppPaths.resolve(tmp_path / "state")
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
                summary_trigger_ratio=None,
                model_retries=0,
                tool_retries=0,
            )
        },
    )
    runtime = await AgentRuntime.open(paths.home)
    options: dict[str, Any] = {}
    if team_enabled is not None:
        options["team_enabled"] = team_enabled
    app = SayacodeApp(
        paths=paths,
        repository=ConfigRepository(paths.home),
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="team-test",
        trust_level="ask",
        profile_name="test",
        model_override=model,
        **options,
    )
    return await app.initialize()


@pytest.mark.parametrize("team_enabled", [None, True, False])
async def test_model_receives_selected_team_tools(
    tmp_path: Path, team_enabled: bool | None
) -> None:
    model = ToolVisibilityModel()
    app = await make_app(tmp_path, model, team_enabled=team_enabled)
    try:
        result = await app.run("了解项目")
        assert result["status"] == "completed"
        assert model.bound_tool_names
        tools = model.bound_tool_names[-1]
        assert {"read_file", "write_todos"} <= tools
        assert "report_to_parent" not in tools
        if team_enabled is False:
            assert tools.isdisjoint(TEAM_TOOLS)
        else:
            assert TEAM_TOOLS <= tools
    finally:
        await app.aclose()


async def test_disabled_team_request_cannot_dispatch(tmp_path: Path) -> None:
    model = ToolVisibilityModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "delegate_to_subagent",
                        "args": {"task": "检查项目"},
                        "id": "disabled-dispatch",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="改为独立处理"),
        ]
    )
    app = await make_app(tmp_path, model, team_enabled=False)
    try:
        result = await app.run("了解项目")
        assert result["status"] == "completed"
        assert await app.tasks.list() == []
        handle, _ = await app._get_handle(thread_id=app.session_id, trust_level="ask")
        snapshot = await app.runtime.get_state(handle, app.session_id)
        messages = snapshot.values["messages"]
        failed_call = next(item for item in messages if isinstance(item, ToolMessage))
        assert failed_call.tool_call_id == "disabled-dispatch"
        assert failed_call.status == "error"
    finally:
        await app.aclose()


async def test_single_agent_sessions_keep_separate_checkpoints(tmp_path: Path) -> None:
    app = await make_app(tmp_path, ToolVisibilityModel(), team_enabled=False)
    try:
        first_thread = app.session_id
        await app.run("第一个项目要求")
        second_thread = await app._new_session()
        await app.run("另一个独立项目要求")
        for thread_id, expected in (
            (first_thread, "第一个项目要求"),
            (second_thread, "另一个独立项目要求"),
        ):
            handle, _ = await app._get_handle(thread_id=thread_id, trust_level="ask")
            snapshot = await app.runtime.get_state(handle, thread_id)
            assert [
                item.content
                for item in snapshot.values["messages"]
                if isinstance(item, HumanMessage)
            ] == [expected]
        assert await app.tasks.list() == []
    finally:
        await app.aclose()


@pytest.mark.parametrize("team_enabled", [True, False])
async def test_child_keeps_reporting_without_nested_dispatch(
    tmp_path: Path, team_enabled: bool
) -> None:
    model = ToolVisibilityModel()
    app = await make_app(tmp_path, model, team_enabled=team_enabled)
    try:
        handle, context = await app._get_handle(
            thread_id="child-test",
            trust_level="ask",
            task_id="task-test",
            agent_role="builder",
            background=True,
            include_team_tools=False,
        )
        await app.runtime.invoke(handle, context, "检查任务", thread_id="child-test")
        tools = model.bound_tool_names[-1]
        assert "report_to_parent" in tools
        assert tools.isdisjoint(TEAM_TOOLS)
    finally:
        await app.aclose()


async def test_application_aligns_prompt_with_team_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """系统提示必须跟随 team_enabled，避免提示教模型使用不存在的团队工具。"""

    import sayacode.application as application_module

    captured: dict[str, Any] = {}
    real = application_module.build_system_prompt

    def capture(*args: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(application_module, "build_system_prompt", capture)

    disabled = tmp_path / "disabled"
    disabled.mkdir()
    app = await make_app(disabled, ToolVisibilityModel(), team_enabled=False)
    try:
        await app.run("了解项目")
    finally:
        await app.aclose()
    assert captured.get("team_enabled") is False

    captured.clear()
    enabled = tmp_path / "enabled"
    enabled.mkdir()
    app = await make_app(enabled, ToolVisibilityModel(), team_enabled=True)
    try:
        await app.run("了解项目")
    finally:
        await app.aclose()
    assert captured.get("team_enabled") is True
