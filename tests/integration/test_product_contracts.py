"""Product contracts exercised through the application and command boundary."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.cli.commands import CommandRouter
from sayacode.extensions.instructions import load_project_instructions
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


async def test_jev_reviewer_configuration_controls_the_session_trust(tmp_path):
    app = await contract_app(tmp_path)
    try:
        with pytest.raises(ValueError, match="reviewer is not configured"):
            await app.command("trust", "jev")
        configured = await app.command(
            "reviewer",
            {
                "action": "setup",
                "config": {
                    "base_url": "https://api.typesafe.test",
                    "api_key": "private-review-key",
                    "model_id": "jev-test",
                },
            },
        )
        assert configured["api_key"] == "***"
        assert (await app.command("trust", "jev"))["trust_level"] == "jev"
        status = await app.command("reviewer", "status")
        assert status["configured"] and status["api_key"] == "***"
        removed = await app.command("reviewer", "remove")
        assert removed["trust_level"] == "ask"
        assert app.config.jev is None
    finally:
        await app.aclose()


async def test_read_only_tool_catalog_hides_mutations_and_shell(tmp_path):
    app = await contract_app(tmp_path)
    try:
        await app.command("trust", "read_only")
        names = {item["name"] for item in await app.command("tools")}
        assert {"read_file", "git", "web_search"} <= names
        assert {
            "write_file",
            "search_replace",
            "delete_file",
            "execute_command_tool",
        }.isdisjoint(names)
    finally:
        await app.aclose()


async def test_store_memory_and_project_instructions_feed_the_next_model_request(tmp_path):
    model = ContractModel()
    app = await contract_app(tmp_path, model)
    try:
        app.paths.instructions.write_text("Prefer reproducible evidence.", encoding="utf-8")
        (app.workspace / "SAYACODE.md").write_text(
            "Use this project convention.", encoding="utf-8"
        )
        await app.command("memory", 'remember user "Use Chinese comments."')
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
        status = await app.command("memory", "status")
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
    app = await contract_app(tmp_path)
    try:
        old = app.workspace / ".sayacode" / "commands" / "inspect.md"
        old.parent.mkdir(parents=True)
        old.write_text("OLD COMMAND", encoding="utf-8")
        router = CommandRouter(app, PromptPreferences())
        result = await router.dispatch("/inspect")
        assert result.prompt is None
        assert "Unknown command" in result.display
        assert "/commands" not in (await router.dispatch("/help")).display
    finally:
        await app.aclose()


async def test_language_survives_config_reload_without_retaining_style(tmp_path):
    app = await contract_app(tmp_path)
    try:
        await app.command("trust", "read_only")
        app.config.preferences["style"] = "catgirl"
        result = await app.command("lang", "中文")
        assert result["language"] == "zh"
        assert app.trust_level == "read_only"
        loaded = await app.repository.load()
        assert loaded.preferences == {"language": "zh"}
        assert "style" not in await app.command("prefs")
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
