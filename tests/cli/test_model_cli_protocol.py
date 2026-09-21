"""The terminal asks for a protocol, not a vendor, and keeps secrets out of history."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import prompt_toolkit
import pytest
from prompt_toolkit.history import History, InMemoryHistory
from rich.console import Console

from sayacode.cli.commands import CommandRouter
from sayacode.cli.display import TerminalPresenter
from sayacode.cli.help import format_help
from sayacode.cli.interactive import _interactive
from sayacode.cli.main import build_parser
from sayacode.prompts import PromptPreferences


def scripted_prompts(
    monkeypatch: pytest.MonkeyPatch, answers: list[str]
) -> list[tuple[str, bool, object]]:
    remaining = iter(answers)
    prompts: list[tuple[str, bool, object]] = []

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            self.history = kwargs.get("history")

        async def prompt_async(self, label: str = "", **kwargs: object) -> str:
            prompts.append((label, bool(kwargs.get("is_password")), self.history))
            try:
                answer = next(remaining)
            except StopIteration:
                raise EOFError from None
            if isinstance(self.history, History) and answer:
                self.history.append_string(answer)
            return answer

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)
    return prompts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "protocol"),
    [
        ("1", "openai_chat_completions"),
        ("2", "openai_responses"),
        ("3", "anthropic_messages"),
        ("4", "gemini_generate_content"),
        ("5", "ollama_native_chat"),
    ],
)
async def test_model_wizard_sends_explicit_protocol_without_exposing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    choice: str,
    protocol: str,
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SAYACODE_HOME", str(state))
    secret = "sk-secret-in-memory-only"
    prompts = scripted_prompts(
        monkeypatch,
        [
            "/model add",
            choice,
            "https://model.example/v1",
            secret,
            "test-model",
            "128k",
            "8k",
            "/quit",
        ],
    )

    class ProfileApp:
        workspace = tmp_path
        session_id = "test-thread"
        trust_level = "ask"
        model = "existing-model"

        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def command(self, name: str, args: Any) -> dict[str, str]:
            self.calls.append((name, args))
            return {"added": "test-model"}

    app = ProfileApp()
    result = await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )
    output, error = capsys.readouterr()

    assert result == 0 and error == ""
    assert app.calls == [
        (
            "model",
            {
                "action": "add",
                "profile": {
                    "protocol": protocol,
                    "base_url": "https://model.example/v1",
                    "api_key": secret,
                    "model_id": "test-model",
                    "context_length": 128000,
                    "max_output_tokens": 8000,
                },
            },
        )
    ]
    assert len([prompt for prompt in prompts if prompt[1]]) == 1
    assert isinstance(next(prompt[2] for prompt in prompts if prompt[1]), InMemoryHistory)
    assert "provider" not in " ".join(prompt[0].lower() for prompt in prompts)
    assert secret not in (state / "input_history").read_text(encoding="utf-8")
    assert secret not in output


@pytest.mark.asyncio
async def test_model_wizard_requires_url_and_positive_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    scripted_prompts(
        monkeypatch,
        [
            "/model add",
            "2",
            "not-a-url",
            "https://model.example/v1",
            "none",
            "model-id",
            "0",
            "64k",
            "bad",
            "4096",
            "/quit",
        ],
    )

    class ProfileApp:
        workspace = tmp_path
        session_id = "thread"
        trust_level = "ask"
        model = "existing"

        def __init__(self) -> None:
            self.payload: Any = None

        async def command(self, name: str, args: Any) -> dict[str, str]:
            assert name == "model"
            self.payload = args
            return {"added": "model-id"}

    app = ProfileApp()
    await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )
    output, _ = capsys.readouterr()
    assert app.payload["profile"]["api_key"] is None
    assert app.payload["profile"]["context_length"] == 64000
    assert app.payload["profile"]["max_output_tokens"] == 4096
    assert "完整的 http(s)" in output
    assert "token count must be a positive number" in output


@pytest.mark.asyncio
async def test_model_wizard_reports_profile_validation_error_and_keeps_cli_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    scripted_prompts(
        monkeypatch,
        [
            "/model add",
            "1",
            "https://model.example/v1?bad=query",
            "none",
            "model-id",
            "64k",
            "8k",
            "/quit",
        ],
    )

    class ProfileApp:
        workspace = tmp_path
        session_id = "thread"
        trust_level = "ask"
        model = "existing"

        async def command(self, name: str, args: Any) -> None:
            raise ValueError("base_url must not contain a query")

    result = await _interactive(
        ProfileApp(),
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )
    output, error = capsys.readouterr()
    assert result == 0 and error == ""
    assert "模型配置未保存" in output
    assert "base_url must not contain a query" in output


@pytest.mark.asyncio
async def test_jev_reviewer_wizard_uses_hidden_key_and_explicit_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SAYACODE_HOME", str(state))
    secret = "typesafe-private-key"
    prompts = scripted_prompts(
        monkeypatch,
        ["/reviewer setup", "", secret, "", "/quit"],
    )

    class ReviewerApp:
        workspace = tmp_path
        session_id = "thread"
        trust_level = "ask"
        model = "configured-model"

        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def command(self, name: str, args: Any) -> dict[str, Any]:
            self.calls.append((name, args))
            return {"configured": True}

    app = ReviewerApp()
    result = await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )
    output, error = capsys.readouterr()
    assert result == 0 and error == ""
    assert app.calls == [
        (
            "reviewer",
            {
                "action": "setup",
                "config": {
                    "base_url": "https://api.typesafe.ai",
                    "api_key": secret,
                    "model_id": "jev-1.13.0",
                },
            },
        )
    ]
    secret_prompts = [entry for entry in prompts if entry[1]]
    assert len(secret_prompts) == 1
    assert isinstance(secret_prompts[0][2], InMemoryHistory)
    assert secret not in (state / "input_history").read_text(encoding="utf-8")
    assert secret not in output


@pytest.mark.asyncio
async def test_positional_model_add_no_longer_calls_app(tmp_path: Path) -> None:
    class App:
        def command(self, name: str, args: Any) -> None:
            raise AssertionError("old positional model configuration reached app")

    result = await CommandRouter(App(), tmp_path, PromptPreferences(language="zh")).dispatch(
        "/model add fast openai gpt-fast"
    )
    assert "/model add" in result.display
    assert "不再接收位置参数" in result.display


def test_model_help_and_list_expose_protocol(capsys: pytest.CaptureFixture[str]) -> None:
    detail = format_help("model", language="zh")
    assert "接口协议" in detail
    assert "工具调用" in detail
    assert "<provider>" not in detail

    presenter = TerminalPresenter(
        Console(force_terminal=False), language="zh", redact=lambda value: value
    )
    presenter.command_result(
        "/models",
        json.dumps(
            {
                "default_profile": "main",
                "profiles": {
                    "main": {
                        "protocol": "openai_responses",
                        "model_id": "model-id",
                        "context_length": 128000,
                        "max_output_tokens": 8192,
                    }
                },
            }
        ),
    )
    output, _ = capsys.readouterr()
    assert "OpenAI Responses" in output
    assert "model-id" in output
    assert "128,000 / 8,192" in output


def test_cli_flags_are_explicit_and_old_model_flags_are_rejected() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--protocol",
            "openai_responses",
            "--base-url",
            "https://model.example/v1",
            "--model-id",
            "model-id",
            "--context-length",
            "128k",
            "--max-output-tokens",
            "8k",
        ]
    )
    assert args.protocol == "openai_responses"
    assert args.context_length == 128000
    assert args.max_output_tokens == 8000
    with pytest.raises(SystemExit):
        parser.parse_args(["--model", "openai:model-id"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--model-type", "openai"])
