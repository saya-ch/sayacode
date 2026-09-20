"""Interactive key input stays hidden and keeps the explicit key source clear."""

from __future__ import annotations

import warnings
from argparse import Namespace
from pathlib import Path
from typing import Any

import prompt_toolkit
import pytest
from langchain_core._api import LangChainBetaWarning
from prompt_toolkit.history import History, InMemoryHistory

from sayacode.cli import _interactive, build_parser
from sayacode.help_catalog import format_help
from sayacode.prompts import PromptPreferences


def _scripted_prompts(
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


async def _run_interactive(tmp_path: Path, app: Any) -> int:
    return await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )


@pytest.mark.asyncio
async def test_wizard_requires_key_and_rejects_environment_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SAYACODE_HOME", str(state))
    prompts = _scripted_prompts(
        monkeypatch,
        [
            "/model add", "1", "https://api.deepseek.com", "",
            "env:DEEPSEEK_API_KEY", "actual-deepseek-key", "deepseek-flash",
            "1M", "256k", "/quit",
        ],
    )

    class App:
        workspace = tmp_path
        session_id = "thread"
        mode = "build"
        model = "existing"

        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def command(self, name: str, args: Any) -> dict[str, str]:
            self.calls.append((name, args))
            return {"added": "deepseek-flash"}

    app = App()
    assert await _run_interactive(tmp_path, app) == 0
    output, error = capsys.readouterr()
    assert not error
    assert app.calls[0][1]["profile"]["api_key"] == "actual-deepseek-key"
    assert "留空不会使用环境变量" in output
    assert "不支持环境变量引用" in output
    assert "actual-deepseek-key" not in output
    assert "env:DEEPSEEK_API_KEY" not in output
    assert "env:DEEPSEEK_API_KEY" not in (state / "input_history").read_text(
        encoding="utf-8"
    )
    hidden = [entry for entry in prompts if entry[1]]
    assert len(hidden) == 3
    assert all(isinstance(entry[2], InMemoryHistory) for entry in hidden)


@pytest.mark.asyncio
async def test_model_key_updates_existing_profile_without_echo_or_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SAYACODE_HOME", str(state))
    secret = "an-unmistakable-secret-value"
    prompts = _scripted_prompts(
        monkeypatch,
        ["/model key deepseek-flash", secret, "/quit"],
    )

    class App:
        workspace = tmp_path
        session_id = "thread"
        mode = "build"
        model = "deepseek-flash"

        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def command(self, name: str, args: Any) -> dict[str, bool]:
            self.calls.append((name, args))
            return {"ok": True}

    app = App()
    assert await _run_interactive(tmp_path, app) == 0
    output, error = capsys.readouterr()
    assert not error
    assert app.calls == [
        (
            "model",
            {"action": "set_key", "name": "deepseek-flash", "api_key": secret},
        )
    ]
    assert secret not in output
    assert secret not in (state / "input_history").read_text(encoding="utf-8")
    assert len([entry for entry in prompts if entry[1]]) == 1
    assert "密钥已更新" in output


@pytest.mark.asyncio
async def test_model_key_requires_value_and_none_explicitly_clears_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    prompts = _scripted_prompts(
        monkeypatch,
        ["/model key local", "", "env:LOCAL_KEY", "none", "/quit"],
    )

    class App:
        workspace = tmp_path
        session_id = "thread"
        mode = "build"
        model = "local"

        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def command(self, name: str, args: Any) -> dict[str, bool]:
            self.calls.append((name, args))
            return {"ok": True}

    app = App()
    assert await _run_interactive(tmp_path, app) == 0
    output, _ = capsys.readouterr()
    assert app.calls == [
        ("model", {"action": "set_key", "name": "local", "api_key": None})
    ]
    assert "不支持环境变量引用" in output
    assert "env:LOCAL_KEY" not in output
    assert len([entry for entry in prompts if entry[1]]) == 3


@pytest.mark.asyncio
async def test_model_key_rejects_inline_secret_without_persisting_or_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SAYACODE_HOME", str(state))
    secret = "inline-secret-that-must-not-be-retained"
    _scripted_prompts(
        monkeypatch,
        [f"/model key deepseek-flash {secret}", "/quit"],
    )

    class App:
        workspace = tmp_path
        session_id = "thread"
        mode = "build"
        model = "deepseek-flash"

        def command(self, _name: str, _args: Any) -> None:
            raise AssertionError("inline secret reached app")

    assert await _run_interactive(tmp_path, App()) == 0
    output, error = capsys.readouterr()
    assert not error
    assert secret not in output
    assert secret not in (state / "input_history").read_text(encoding="utf-8")
    assert "隐藏输入框" in output


@pytest.mark.asyncio
async def test_model_key_redacts_secret_from_save_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    secret = "secret-that-appears-in-error"
    _scripted_prompts(monkeypatch, ["/model key remote", secret, "/quit"])

    class App:
        workspace = tmp_path
        session_id = "thread"
        mode = "build"
        model = "remote"

        def command(self, _name: str, _args: Any) -> None:
            raise ValueError(f"Rejected {secret}")

    assert await _run_interactive(tmp_path, App()) == 0
    output, error = capsys.readouterr()
    assert not error
    assert secret not in output
    assert "Rejected ***" in output


@pytest.mark.asyncio
async def test_model_add_redacts_secret_from_save_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    secret = "secret-in-save-error"
    _scripted_prompts(
        monkeypatch,
        [
            "/model add", "1", "https://models.example/v1", secret,
            "model", "8192", "1024", "/quit",
        ],
    )

    class App:
        workspace = tmp_path
        session_id = "thread"
        mode = "build"
        model = "existing"

        def command(self, _name: str, _args: Any) -> dict[str, Any]:
            return {"ok": False, "error": f"Rejected {secret}"}

    assert await _run_interactive(tmp_path, App()) == 0
    output, _ = capsys.readouterr()
    assert secret not in output
    assert "Rejected ***" in output


@pytest.mark.asyncio
async def test_interactive_filters_only_v3_beta_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _scripted_prompts(monkeypatch, ["hello", "/quit"])

    class App:
        workspace = tmp_path
        session_id = "thread"
        mode = "build"
        model = "model"

        async def stream(self, _prompt: str, **_kwargs: Any) -> Any:
            warnings.warn(
                "The v3 streaming protocol on Pregel is experimental.",
                LangChainBetaWarning,
                stacklevel=2,
            )
            warnings.warn("unrelated warning", UserWarning, stacklevel=2)
            yield {"type": "run.completed", "response": "hello"}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert await _run_interactive(tmp_path, App()) == 0
    assert [(str(item.message), item.category) for item in caught] == [
        ("unrelated warning", UserWarning)
    ]


def test_model_help_documents_required_key_and_explicit_keyless_choice() -> None:
    detail = format_help("model", language="zh")
    assert "/model key <名称>" in detail
    assert "API Key 默认必填" in detail
    assert "输入 none" in detail
    assert "env:变量名" not in detail


def test_one_shot_authentication_flags_are_mutually_exclusive() -> None:
    parser = build_parser()
    assert parser.parse_args(["--no-api-key"]).no_api_key is True
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--api-key", "test", "--no-api-key"])
    assert exc.value.code == 2
