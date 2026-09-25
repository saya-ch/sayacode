"""Skill 的发现、路径边界与真实 LangGraph 工具链。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain.tools import ToolRuntime
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from sayacode.agent import AgentContext, AgentRuntime
from sayacode.agent.events import EventProjector
from sayacode.config import Profile
from sayacode.extensions.skills import (
    SkillRegistry,
    SkillsMiddleware,
    skill_budget_bytes,
    skill_tools,
)
from tests.support import ContractModel, contract_app


def _write_skill(root: Path, name: str, body: str, *, description: str = "检查代码") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _profile() -> Profile:
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
        summary_trigger_ratio=None,
        model_retries=0,
        tool_retries=0,
    )


class SkillCallingModel(BaseChatModel):
    systems: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "skill-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        system = next(
            (str(message.content) for message in messages if message.type == "system"), ""
        )
        self.systems.append(system)
        if not any(isinstance(message, ToolMessage) for message in messages):
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": "load_skill", "args": {"name": "code-review"}, "id": "call-skill"}
                ],
            )
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_skill_registry_discovers_metadata_and_project_override(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user = tmp_path / "home" / "skills"
    project = workspace / ".agents" / "skills"
    _write_skill(user, "code-review", "用户正文", description="用户版本")
    selected = _write_skill(project, "code-review", "项目正文", description="项目版本")
    _write_skill(user, "data-analysis", "分析正文", description="分析数据")
    registry = SkillRegistry(user, workspace)

    catalog = registry.list()
    assert [(item.name, item.source) for item in catalog] == [
        ("code-review", "project"),
        ("data-analysis", "user"),
    ]
    assert catalog[0].description == "项目版本"
    assert catalog[0].path == str(selected)
    assert registry.activate("code-review").content == "项目正文"
    assert registry.read("code-review").startswith("---\nname: code-review")

    child_workspace = tmp_path / "child-workspace"
    _write_skill(child_workspace / ".agents" / "skills", "child-only", "子工作区正文")
    assert [item.name for item in registry.list(child_workspace)] == [
        "child-only",
        "code-review",
        "data-analysis",
    ]
    assert registry.activate("child-only", child_workspace).content == "子工作区正文"


def test_skill_registry_validates_metadata_and_resource_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".agents" / "skills"
    skill = _write_skill(root, "safe-skill", "使用 references/guide.md")
    reference = skill.parent / "references" / "guide.md"
    reference.parent.mkdir()
    reference.write_text("项目内部参考", encoding="utf-8")
    _write_skill(root, "bad_name", "应被忽略")
    registry = SkillRegistry(tmp_path / "home" / "skills", workspace)

    assert [item.name for item in registry.list()] == ["safe-skill"]
    assert registry.read_resource("safe-skill", "references/guide.md") == "项目内部参考"
    with pytest.raises(ValueError, match="load_skill"):
        registry.read_resource("safe-skill", "SKILL.md")
    assert registry.read_resource("safe-skill", "references/guide.md", max_bytes=6).endswith(
        "read_file 分段读取该文件]"
    )
    with pytest.raises(ValueError, match="相对路径"):
        registry.read_resource("safe-skill", "../private.txt")

    outside = tmp_path / "outside.txt"
    outside.write_text("不允许的引用", encoding="utf-8")
    try:
        (skill.parent / "references" / "outside.md").symlink_to(outside)
    except OSError:
        pytest.skip("当前文件系统不允许创建符号链接")
    with pytest.raises(ValueError, match="超出目录"):
        registry.read_resource("safe-skill", "references/outside.md")


def test_project_skill_directory_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    _write_skill(outside / "skills", "external", "不允许的正文")
    workspace.mkdir()
    try:
        (workspace / ".agents").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前文件系统不允许创建符号链接")
    registry = SkillRegistry(tmp_path / "home" / "skills", workspace)
    assert registry.list() == []
    with pytest.raises(KeyError, match="未找到"):
        registry.activate("external")


@pytest.mark.asyncio
async def test_skill_tool_activates_in_checkpoint_and_survives_reopen(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(
        workspace / ".agents" / "skills",
        "code-review",
        "检查路径处理和异常分支，并报告验证证据。",
    )
    registry = SkillRegistry(tmp_path / "home" / "skills", workspace)
    context = AgentContext(
        workspace=workspace,
        trust_level="read_only",
        policy=None,
        output_dir=tmp_path,
        session_id="skill-thread",
    )
    model = SkillCallingModel()
    state_root = tmp_path / "state"

    async with await AgentRuntime.open(state_root) as runtime:
        handle = runtime.build_agent(
            _profile(),
            skill_tools(registry),
            context=context,
            model_override=model,
            extra_middleware=[SkillsMiddleware(registry)],
        )
        projector = EventProjector()
        projected: list[dict[str, object]] = []
        run = await runtime.open_event_stream_v3(handle, context, "请检查代码")
        async with run:
            async for event in run:
                projected.extend(projector.normalize(event, context.session_id))
            result = await run.output()
        assert result["messages"][-1].content == "done"
        assert result["active_skills"]["code-review"].startswith("检查路径处理")
        assert "检查路径处理" not in str(projected)
        assert "检查路径处理" not in model.systems[0]
        assert "code-review: 检查代码" in model.systems[0]
        assert "检查路径处理" in model.systems[1]

    resumed_model = SkillCallingModel()
    async with await AgentRuntime.open(state_root) as runtime:
        handle = runtime.build_agent(
            _profile(),
            skill_tools(registry),
            context=context,
            model_override=resumed_model,
            extra_middleware=[SkillsMiddleware(registry)],
        )
        await runtime.invoke(handle, context, "继续检查")
        assert "检查路径处理" in resumed_model.systems[0]


@pytest.mark.asyncio
async def test_cli_activation_and_read_only_tool_catalog(tmp_path: Path) -> None:
    model = ContractModel()
    app = await contract_app(tmp_path, model=model)
    try:
        await app._save_thread_policy(app.session_id, trust_level="read_only")
        _write_skill(app.workspace / ".agents" / "skills", "code-review", "检查异常分支")
        activation = await app.activate_skill("code-review")
        assert activation.name == "code-review"
        assert activation.source == "project"
        assert (await app.activate_skill("code-review")).name == "code-review"

        handle, context = await app._get_handle(thread_id=app.session_id, trust_level="read_only")
        tool_names = {item.name for item in app._tools_for_context(context)}
        assert {"list_skills", "load_skill", "read_skill_resource"} <= tool_names
        assert "execute_command_tool" not in tool_names
        snapshot = await app.runtime.get_state(handle, app.session_id)
        assert snapshot.values["active_skills"]["code-review"] == "检查异常分支"
        assert (await app.run("按刚刚激活的 Skill 检查代码"))["status"] == "completed"
        assert "检查异常分支" in str(model.received[-1][0].content)
        assert (await app.activate_skill("code-review")).name == "code-review"
        assert (await app.run("继续检查"))["status"] == "completed"
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_skill_activation_budget_is_shared_by_cli_and_agent_tool(tmp_path: Path) -> None:
    app = await contract_app(tmp_path)
    try:
        root = app.workspace / ".agents" / "skills"
        _write_skill(root, "first-skill", "a" * 2_000)
        _write_skill(root, "second-skill", "b" * 2_000)
        _write_skill(root, "oversized-skill", "c" * (17 * 1024))
        budget = skill_budget_bytes(app.config.profile())
        assert 2_000 < budget < 4_000

        with pytest.raises(ValueError, match="过大"):
            await app.activate_skill("oversized-skill")
        await app.activate_skill("first-skill")
        with pytest.raises(ValueError, match="上下文预算"):
            await app.activate_skill("second-skill")

        loader = next(
            item
            for item in skill_tools(app.skills, max_active_bytes=budget)
            if item.name == "load_skill"
        )
        runtime = ToolRuntime(
            state={"active_skills": {"first-skill": "a" * 2_000}},
            context=app._context(app.session_id, "ask"),
            config={},
            stream_writer=lambda _data: None,
            tool_call_id="load-skill-budget",
            store=app.runtime.store,
            tools=[loader],
        )
        assert loader.coroutine is not None
        with pytest.raises(ValueError, match="上下文预算"):
            await loader.coroutine(name="second-skill", runtime=runtime)

        resource_tool = next(
            item for item in skill_tools(app.skills) if item.name == "read_skill_resource"
        )
        assert resource_tool.coroutine is not None
        with pytest.raises(ValueError, match="先使用 load_skill"):
            await resource_tool.coroutine(
                name="second-skill", path="references/guide.md", runtime=runtime
            )
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_large_skill_catalog_is_bounded_and_searchable(tmp_path: Path) -> None:
    model = ContractModel()
    app = await contract_app(tmp_path, model=model)
    try:
        root = app.workspace / ".agents" / "skills"
        for index in range(30):
            _write_skill(root, f"skill-{index:02d}", "按需正文", description="x" * 1_024)
        await app.run("列出可用方法")
        system = str(model.received[-1][0].content)
        assert "list_skills(query, offset)" in system
        assert system.count("x" * 1_024) <= 2

        listing = next(item for item in skill_tools(app.skills) if item.name == "list_skills")
        runtime = ToolRuntime(
            state={},
            context=app._context(app.session_id, "ask"),
            config={},
            stream_writer=lambda _data: None,
            tool_call_id="list-skills-page",
            store=app.runtime.store,
            tools=[listing],
        )
        assert listing.coroutine is not None
        page = await listing.coroutine(runtime=runtime, query="skill-", offset=10, limit=10)
        assert page["total"] == 30
        assert page["skills"][0]["name"] == "skill-10"
        assert len(page["skills"]) == 10
        assert max(len(item["description"]) for item in page["skills"]) == 160
    finally:
        await app.aclose()
