"""Product contracts exercised through the application and command boundary."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.cli.commands import CommandRouter
from sayacode.extensions.custom_commands import discover_custom_commands
from sayacode.extensions.memory import load_project_instructions
from sayacode.prompts import PromptPreferences
from tests.support import ContractModel, contract_app


async def test_model_profiles_persist_and_command_output_redacts_credentials(tmp_path):
    app = await contract_app(tmp_path)
    try:
        added = await app.command(
            "model",
            {
                "action": "add",
                "profile": {
                    "protocol": "openai_chat_completions",
                    "base_url": "https://model.invalid/v1",
                    "api_key": "secret-test-value",
                    "model_id": "local-model",
                    "context_length": 8192,
                    "max_output_tokens": 512,
                },
            },
        )
        alias = added["added"]
        await app.command("model", f"use {alias}")
        visible = await app.command("config", "list")
        assert visible["profiles"][alias]["api_key"] == "***"
        assert "secret-test-value" not in json.dumps(visible)
        loaded = await app.repository.load()
        assert loaded.default_profile == alias
        assert loaded.profile(alias).api_key == "secret-test-value"
        await app.command("model", f"delete {alias}")
        loaded = await app.repository.load()
        assert alias not in loaded.profiles and loaded.default_profile == "test"
    finally:
        await app.aclose()


async def test_model_test_honors_the_requested_profile(tmp_path, monkeypatch):
    app = await contract_app(tmp_path)
    try:
        added = await app.command(
            "model",
            {
                "action": "add",
                "profile": {
                    "protocol": "openai_chat_completions",
                    "base_url": "https://model.invalid/v1",
                    "api_key": "test-key",
                    "model_id": "alternate-model",
                    "context_length": 8192,
                    "max_output_tokens": 512,
                },
            },
        )
        alias = added["added"]
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

        monkeypatch.setattr("sayacode.agent.models.model_for", model_for)
        result = await app.command("model", f"test {alias}")
        assert result["ok"] is True
        assert result["text"] and result["tool_calling"] and result["stream"]
        assert tested == [alias]
        assert app.config.default_profile == "test"
    finally:
        await app.aclose()


async def test_session_switch_isolates_history_and_survives_reopening(tmp_path):
    app = await contract_app(
        tmp_path,
        ContractModel(
            responses=[AIMessage(content="first answer"), AIMessage(content="second answer")]
        ),
    )
    first = app.session_id
    try:
        assert (await app.run("first question"))["ok"]
        new = await app.command("session", 'new "Separate investigation"')
        second = new["session_id"]
        assert second != first
        assert (await app.command("session", "current"))["title"] == "Separate investigation"
        assert await app.command("history") == []
        assert (await app.run("second question"))["ok"]
        await app.command("session", f"use {first}")
        assert [m["content"] for m in await app.command("history") if m["role"] == "human"] == [
            "first question"
        ]
    finally:
        await app.aclose()
    reopened = await contract_app(tmp_path, session_id=first)
    try:
        await reopened.command("session", f"use {second}")
        assert [
            m["content"] for m in await reopened.command("history") if m["role"] == "human"
        ] == ["second question"]
        sessions = await reopened.command("session", "list")
        assert {first, second}.issubset({item["thread_id"] for item in sessions})
    finally:
        await reopened.aclose()


async def test_session_trust_and_default_for_new_sessions_persist(tmp_path):
    app = await contract_app(tmp_path)
    try:
        await app.command("trust", "full")
        await app.command("trust", "default read_only")
    finally:
        await app.aclose()
    reopened = await contract_app(tmp_path)
    try:
        assert (await reopened.command("trust"))["trust_level"] == "full"
        assert (await reopened.command("trust"))["default_trust"] == "read_only"
        fresh = await reopened.command("session", "new")
        assert (await reopened.runtime.get_thread(fresh["session_id"]))[
            "trust_level"
        ] == "read_only"
    finally:
        await reopened.aclose()


async def test_read_only_tool_catalog_hides_mutations_but_keeps_approved_shell(tmp_path):
    app = await contract_app(tmp_path)
    try:
        await app.command("trust", "read_only")
        names = {item["name"] for item in await app.command("tools")}
        assert {"read_file", "git", "web_search", "execute_command_tool"} <= names
        assert {"write_file", "search_replace", "delete_file"}.isdisjoint(names)
    finally:
        await app.aclose()


async def test_user_and_project_memory_feed_the_next_model_request(tmp_path):
    model = ContractModel()
    app = await contract_app(tmp_path, model)
    try:
        await app.command("memory", 'append user "Prefer reproducible evidence."')
        await app.command("memory", 'append project "Use this project convention."')
        assert (await app.run("inspect"))["ok"]
        system = str(model.received[0][0].content)
        assert "Prefer reproducible evidence." in system
        assert "Use this project convention." in system
        status = await app.command("memory", "status")
        assert status["loaded_characters"] > 0
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


async def test_custom_command_expansion_routes_into_real_app_without_shell_execution(
    tmp_path, monkeypatch
):
    model = ContractModel()
    app = await contract_app(tmp_path, model)
    monkeypatch.setenv("SAYACODE_HOME", str(app.paths.home))
    try:
        project_commands = app.paths.project_commands(app.workspace) / "ops"
        project_commands.mkdir(parents=True)
        project_commands.joinpath("inspect.md").write_text(
            "---\ndescription: local inspection\n---\nInspect $1; tenth=$10; all=$ARGUMENTS; missing=$11\n",
            encoding="utf-8",
        )
        app.paths.user_commands.joinpath("inspect.md").write_text("USER VERSION", encoding="utf-8")
        commands = discover_custom_commands(app.workspace, home=app.paths.home)
        assert commands["/inspect"].scope == "project"
        router = CommandRouter(app, app.workspace, PromptPreferences())
        result = await router.dispatch('/ops:inspect "two words" b c d e f g h i j')
        assert result.prompt is not None
        assert "Inspect two words; tenth=j;" in result.prompt
        assert "$11" not in result.prompt
        assert (await app.run(result.prompt))["ok"]
        assert model.received[0][-1].content == result.prompt
        assert "ops:inspect" in (await router.dispatch("/commands")).display
    finally:
        await app.aclose()


@pytest.mark.parametrize(
    "name,value,expected", [("style", "猫娘", "catgirl"), ("lang", "中文", "zh")]
)
async def test_style_language_survive_config_reload_without_changing_trust(
    tmp_path, name, value, expected
):
    app = await contract_app(tmp_path)
    try:
        await app.command("trust", "read_only")
        result = await app.command(name, value)
        assert expected in result.values()
        assert app.trust_level == "read_only"
        loaded = await app.repository.load()
        assert loaded.preferences["style" if name == "style" else "language"] == expected
    finally:
        await app.aclose()


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
