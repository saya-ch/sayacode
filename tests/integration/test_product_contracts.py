"""通过 Web 宿主、原生图和产品服务验证用户可见行为。"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.config import Config, ConfigRepository, Profile
from sayacode.extensions.instructions import load_project_instructions
from sayacode.host.application import WebHost
from tests.support import ContractModel, contract_app


async def _host(tmp_path: Path, model: ContractModel | None = None) -> WebHost:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    repository = ConfigRepository(tmp_path / "state")
    if not repository.path.exists():
        await repository.save(
            Config(
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
                        summary_trigger_ratio=None,
                        tool_selector_max_tools=None,
                        model_retries=0,
                        tool_retries=0,
                    )
                },
            )
        )
    host = await WebHost.open(workspace, home=tmp_path / "state")
    if model is not None:
        assert host.initial_workspace_id is not None
        (await host._app_for_workspace(host.initial_workspace_id)).model_override = model
    return host


async def _run(host: WebHost, thread_id: str, message: str) -> dict:
    receipt = await host.start_run(thread_id, message)
    assert receipt["status"] == "running"
    await asyncio.wait_for(host._runs[thread_id].task, timeout=15)
    return await host.thread_snapshot(thread_id)


async def test_model_profiles_persist_and_web_output_redacts_credentials(tmp_path):
    host = await _host(tmp_path)
    try:
        added = await host.create_profile(
            {
                "protocol": "openai_chat_completions",
                "base_url": "https://model.invalid/v1",
                "api_key": "secret-test-value",
                "model_id": "local-model",
                "context_length": 8192,
                "max_output_tokens": 512,
            }
        )
        alias = added["name"]
        assert (await host.select_profile(alias))["active_profile"] == alias
        visible = await host.list_profiles()
        assert next(item for item in visible["profiles"] if item["name"] == alias)["has_api_key"]
        assert "secret-test-value" not in json.dumps(visible)
        loaded = await host.repository.load()
        assert loaded.default_profile == alias
        assert loaded.profile(alias).api_key == "secret-test-value"
        await host.delete_profile(alias)
        loaded = await host.repository.load()
        assert alias not in loaded.profiles and loaded.default_profile == "test"
    finally:
        await host.aclose()


async def test_model_test_honors_the_requested_profile(tmp_path, monkeypatch):
    host = await _host(tmp_path)
    try:
        added = await host.create_profile(
            {
                "protocol": "openai_chat_completions",
                "base_url": "https://model.invalid/v1",
                "api_key": "test-key",
                "model_id": "alternate-model",
                "context_length": 8192,
                "max_output_tokens": 512,
            }
        )
        alias = added["name"]
        tested = []

        class CapabilityModel(ContractModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                if "sayacode_capability_probe" in str(messages[-1].content):
                    return ChatResult(
                        generations=[
                            ChatGeneration(
                                message=AIMessage(
                                    content="",
                                    tool_calls=[
                                        {
                                            "name": "sayacode_capability_probe",
                                            "args": {"value": "ping"},
                                            "id": "call-probe",
                                        }
                                    ],
                                )
                            )
                        ]
                    )
                return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        def model_for(profile, override=None):
            tested.append(profile.name)
            return CapabilityModel()

        monkeypatch.setattr("sayacode.host.products.model_for", model_for)
        result = await host.test_profile(alias)
        assert result["ok"] is True
        assert result["text"] and result["tool_calling"] and result["stream"]
        assert tested == [alias]
        assert host.config.default_profile == "test"
    finally:
        await host.aclose()


async def test_session_switch_isolates_history_and_survives_reopening(tmp_path):
    host = await _host(
        tmp_path,
        ContractModel(
            responses=[AIMessage(content="first answer"), AIMessage(content="second answer")]
        ),
    )
    assert host.initial_workspace_id is not None
    workspace_id = host.initial_workspace_id
    first = (await host.list_sessions(workspace_id))[0]["id"]
    try:
        assert (await _run(host, first, "first question"))["messages"][-1]["text"] == "first answer"
        new = await host.create_session(workspace_id, "Separate investigation")
        second = new["id"]
        assert second != first
        assert (await host.thread_snapshot(second))["title"] == "Separate investigation"
        assert (await host.thread_snapshot(second))["messages"] == []
        assert (await _run(host, second, "second question"))["messages"][-1]["text"] == "second answer"
        assert [m["text"] for m in (await host.thread_snapshot(first))["messages"] if m["role"] == "human"] == [
            "first question"
        ]
    finally:
        await host.aclose()
    reopened = await _host(tmp_path)
    try:
        assert reopened.initial_workspace_id is not None
        assert [
            m["text"] for m in (await reopened.thread_snapshot(second))["messages"] if m["role"] == "human"
        ] == ["second question"]
        sessions = await reopened.list_sessions(reopened.initial_workspace_id)
        assert {first, second}.issubset({item["id"] for item in sessions})
    finally:
        await reopened.aclose()


async def test_session_trust_and_default_for_new_sessions_persist(tmp_path):
    host = await _host(tmp_path)
    assert host.initial_workspace_id is not None
    workspace_id = host.initial_workspace_id
    first = (await host.list_sessions(workspace_id))[0]["id"]
    try:
        await host.set_trust(first, "full")
        await host.update_settings({"default_trust": "read_only"})
    finally:
        await host.aclose()
    reopened = await _host(tmp_path)
    try:
        assert (await reopened.thread_snapshot(first))["trust_level"] == "full"
        assert (await reopened.settings())["default_trust"] == "read_only"
        assert reopened.initial_workspace_id is not None
        fresh = await reopened.create_session(reopened.initial_workspace_id, None)
        assert (await reopened.runtime.get_thread(fresh["id"]))[
            "trust_level"
        ] == "read_only"
    finally:
        await reopened.aclose()


async def test_jev_reviewer_configuration_controls_the_session_trust(tmp_path):
    host = await _host(tmp_path)
    assert host.initial_workspace_id is not None
    thread_id = (await host.list_sessions(host.initial_workspace_id))[0]["id"]
    try:
        with pytest.raises(ValueError, match="尚未配置 Jev"):
            await host.set_trust(thread_id, "jev")
        configured = await host.configure_reviewer(
            {
                "base_url": "https://api.typesafe.test",
                "api_key": "private-review-key",
                "model_id": "jev-test",
            }
        )
        assert configured["has_api_key"] and "api_key" not in configured
        assert (await host.set_trust(thread_id, "jev"))["trust_level"] == "jev"
        status = await host.reviewer_status()
        assert status["configured"] and status["has_api_key"]
        assert "private-review-key" not in json.dumps(status)
        with pytest.raises(ValueError, match="先切换信任档"):
            await host.remove_reviewer()
        await host.set_trust(thread_id, "ask")
        removed = await host.remove_reviewer()
        assert removed["configured"] is False
        assert host.config.jev is None
    finally:
        await host.aclose()


async def test_read_only_tool_catalog_hides_mutations_and_shell(tmp_path):
    host = await _host(tmp_path)
    try:
        assert host.initial_workspace_id is not None
        app = await host._app_for_workspace(host.initial_workspace_id)
        await host.set_trust(app.session_id, "read_only")
        context = app._context(app.session_id, "read_only")
        names = {item.name for item in app._tools_for_context(context)}
        assert {"read_file", "git", "web_search"} <= names
        assert {
            "write_file",
            "search_replace",
            "delete_file",
            "execute_command_tool",
        }.isdisjoint(names)
    finally:
        await host.aclose()


async def test_store_memory_and_project_instructions_feed_the_next_model_request(tmp_path):
    model = ContractModel()
    app = await contract_app(tmp_path, model)
    try:
        app.paths.instructions.write_text("Prefer reproducible evidence.", encoding="utf-8")
        (app.workspace / "SAYACODE.md").write_text(
            "Use this project convention.", encoding="utf-8"
        )
        await app.memory.remember("Use Chinese comments.", "user")
        assert (await app.run("inspect"))["ok"]
        system = str(model.received[0][0].content)
        assert "Prefer reproducible evidence." in system
        assert "Use this project convention." in system
        assert "Use Chinese comments." not in system
        assert any(
            "Use Chinese comments." in str(message.content)
            for message in model.received[0][1:]
        )
        assert "Background tasks are asynchronous" in system
        assert "write_todos" in system
        status = await app.memory.status()
        assert status["counts"]["active"] == 1
    finally:
        await app.aclose()


async def test_instruction_file_change_refreshes_the_next_model_request(tmp_path):
    model = ContractModel()
    app = await contract_app(tmp_path, model)
    try:
        app.paths.instructions.write_text("第一版用户说明", encoding="utf-8")
        assert (await app.run("第一轮"))["status"] == "completed"
        app.paths.instructions.write_text("第二版用户说明", encoding="utf-8")
        assert (await app.run("第二轮"))["status"] == "completed"
        first = str(model.received[0][0].content)
        second = str(model.received[1][0].content)
        assert "第一版用户说明" in first
        assert "第二版用户说明" not in first
        assert "第二版用户说明" in second
        assert "第一版用户说明" not in second
    finally:
        await app.aclose()


def test_memory_imports_follow_local_references_and_still_block_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SAYACODE.md").write_text(
        "Primary rules\n@./detail.md\n@./.env\n@./../outside.md\n", encoding="utf-8"
    )
    (workspace / "detail.md").write_text("Local imported rules\n@./SAYACODE.md", encoding="utf-8")
    (workspace / ".env").write_text("SECRET_MUST_NOT_APPEAR=abc", encoding="utf-8")
    (tmp_path / "outside.md").write_text("OUTSIDE_MUST_NOT_APPEAR", encoding="utf-8")
    result = load_project_instructions(workspace)
    assert "Primary rules" in result and "Local imported rules" in result
    assert result.count("Primary rules") == 1
    assert "SECRET_MUST_NOT_APPEAR" in result
    assert "OUTSIDE_MUST_NOT_APPEAR" not in result


async def test_removed_markdown_commands_are_not_exposed(tmp_path):
    host = await _host(tmp_path)
    try:
        assert host.initial_workspace_id is not None
        app = await host._app_for_workspace(host.initial_workspace_id)
        old = app.workspace / ".sayacode" / "commands" / "inspect.md"
        old.parent.mkdir(parents=True)
        old.write_text("OLD COMMAND", encoding="utf-8")
        assert await host.list_skills(host.initial_workspace_id) == []
    finally:
        await host.aclose()


async def test_language_survives_config_reload_without_retaining_style(tmp_path):
    host = await _host(tmp_path)
    try:
        assert host.initial_workspace_id is not None
        app = await host._app_for_workspace(host.initial_workspace_id)
        await host.set_trust(app.session_id, "read_only")
        app.config.preferences["style"] = "catgirl"
        result = await host.update_settings({"language": "zh"})
        assert result["language"] == "zh"
        assert (await host.thread_snapshot(app.session_id))["trust_level"] == "read_only"
        loaded = await host.repository.load()
        assert loaded.preferences == {"language": "zh"}
        assert "style" not in await host.settings()
    finally:
        await host.aclose()


def test_new_package_has_no_legacy_or_deep_agents_imports():
    root = Path(__file__).resolve().parents[2] / "src" / "sayacode"
    forbidden = {"lib", "deepagents", "langgraph_supervisor"}
    violations = []
    for source in root.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            if any(name.split(".")[0] in forbidden for name in names):
                violations.append(f"{source.name}:{node.lineno}")
    assert violations == []
