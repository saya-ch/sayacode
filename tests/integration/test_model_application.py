"""Model profiles and capability probes through the real app boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.application import create_app
from sayacode.cli.main import amain, build_parser
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.host.application import WebHost
from sayacode.paths import AppPaths


class CapabilityModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "capability-probe-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        prompt = str(messages[-1].content)
        if "sayacode_capability_probe" in prompt:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "sayacode_capability_probe",
                        "args": {"value": "ping"},
                        "id": "probe-1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            answer = AIMessage(content="OK")
        return ChatResult(generations=[ChatGeneration(message=answer)])

    def bind_tools(self, tools, **kwargs):
        return self


def endpoint(model_id: str = "coder") -> dict[str, object]:
    return {
        "protocol": "openai_chat_completions",
        "base_url": "https://models.example.invalid/v1",
        "api_key": None,
        "model_id": model_id,
        "context_length": 8192,
        "max_output_tokens": 1024,
    }


async def test_model_add_auto_names_and_capability_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = AppPaths.resolve(tmp_path / "state")
    repository = ConfigRepository(paths.home)
    await repository.save(Config())
    host = await WebHost.open(workspace, home=paths.home)
    monkeypatch.setattr("sayacode.host.products.model_for", lambda _profile: CapabilityModel())
    try:
        first = await host.create_profile(endpoint())
        second = await host.create_profile(endpoint())
        assert first["name"] == "coder"
        assert second["name"] == "coder-2"
        listed = await host.list_profiles()
        assert {item["name"] for item in listed["profiles"]} == {"coder", "coder-2"}
        assert listed["profiles"][0]["protocol"] == "openai_chat_completions"
        assert listed["active_profile"] == "coder"
        report = await host.test_profile("coder")
        assert report["ok"] is True
        assert report["text"] and report["tool_calling"] and report["stream"]
    finally:
        await host.aclose()


async def test_six_field_one_run_profile_and_partial_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    args = build_parser().parse_args(
        [
            "--workspace",
            str(tmp_path),
            "--protocol",
            "ollama_native_chat",
            "--base-url",
            "http://127.0.0.1:11434",
            "--model-id",
            "llama:latest",
            "--context-length",
            "8192",
            "--max-output-tokens",
            "1024",
            "--no-api-key",
        ]
    )
    app = await create_app(args)
    try:
        assert app.model == "llama:latest"
        profile = app._profile()
        assert profile.protocol == "ollama_native_chat"
        assert profile.context_length == 8192
        assert profile.max_output_tokens == 1024
        assert app.protocol == "ollama_native_chat"
    finally:
        await app.aclose()
    incomplete = build_parser().parse_args(
        [
            "--workspace",
            str(tmp_path),
            "--protocol",
            "openai_responses",
        ]
    )
    with pytest.raises(ValueError, match="missing"):
        await create_app(incomplete)
    missing_key = build_parser().parse_args(
        [
            "--workspace",
            str(tmp_path),
            "--protocol",
            "ollama_native_chat",
            "--base-url",
            "http://127.0.0.1:11434",
            "--model-id",
            "llama:latest",
            "--context-length",
            "8192",
            "--max-output-tokens",
            "1024",
        ]
    )
    with pytest.raises(ValueError, match="--api-key.*--no-api-key"):
        await create_app(missing_key)


async def test_named_profile_test_ignores_one_run_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = AppPaths.resolve(tmp_path / "state")
    profile = Profile(name="saved", **endpoint("saved-model"))
    config = Config(default_profile="saved", profiles={"saved": profile})
    repository = ConfigRepository(paths.home)
    await repository.save(config)
    host = await WebHost.open(workspace, home=paths.home)
    app = await host._app_for_workspace(str(host.initial_workspace_id))
    app.profile_override = Profile(name="temporary", **endpoint("temp"))
    app.model_override = CapabilityModel()
    overrides: list[object] = []

    def model_for(chosen: Profile):
        overrides.append(chosen.name)
        return CapabilityModel()

    monkeypatch.setattr("sayacode.host.products.model_for", model_for)
    try:
        result = await host.test_profile("saved")
        assert result["ok"]
        assert overrides == ["saved"]
        await host.select_profile("saved")
        assert app.profile_override is None
        assert app.model == "saved-model"
    finally:
        await host.aclose()


async def test_invalid_saved_model_config_exits_as_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "default_profile": "legacy",
                "profiles": {"legacy": {"name": "legacy", "provider": "openai", "model": "x"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    code = await amain(["--workspace", str(tmp_path), "-p", "hello", "--output-format", "json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["status"] == "config_error"
    assert str(home / "config.json") in output["error"]
