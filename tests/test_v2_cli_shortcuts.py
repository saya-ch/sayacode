"""User-facing shortcuts for sessions and model profiles."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import prompt_toolkit
import pytest
from prompt_toolkit.history import History, InMemoryHistory

from sayacode.cli import _interactive
from sayacode.commands import CommandRouter
from sayacode.config import Config, Profile
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


@pytest.mark.asyncio
async def test_new_creates_and_activates_a_session_from_interactive_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _scripted_prompts(monkeypatch, ["/new", "/quit"])

    class SessionApp:
        workspace = tmp_path
        session_id = "old-thread"
        mode = "build"
        model = "test-model"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def command(self, name: str, args: str) -> dict[str, str]:
            self.calls.append((name, args))
            if name == "session" and args == "new":
                self.session_id = "new-thread"
                return {"session_id": self.session_id}
            raise AssertionError((name, args))

    app = SessionApp()
    result = await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )
    out, err = capsys.readouterr()

    assert result == 0 and err == ""
    assert app.calls == [("session", "new")]
    assert app.session_id == "new-thread"
    assert "new-thread" in out


@pytest.mark.asyncio
async def test_models_lists_saved_profiles_without_invoking_a_model(tmp_path: Path) -> None:
    class ProfileApp:
        workspace = tmp_path

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def command(self, name: str, args: str) -> dict[str, object]:
            self.calls.append((name, args))
            if name == "model" and args == "list":
                return {
                    "default_profile": "deepseek",
                    "profiles": {"deepseek": {"model": "deepseek-chat"}},
                }
            raise AssertionError((name, args))

    app = ProfileApp()
    result = await CommandRouter(app, tmp_path, PromptPreferences()).dispatch("/models")

    assert app.calls == [("model", "list")]
    assert "deepseek" in result.display
    assert "deepseek-chat" in result.display
    assert result.prompt is None


@pytest.mark.asyncio
async def test_model_add_without_arguments_opens_secret_wizard_even_with_a_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SAYACODE_HOME", str(state))
    secret = "sk-test-do-not-store-in-history"
    prompts = _scripted_prompts(
        monkeypatch,
        ["/model add", "extra", "openai", "gpt-test", "", secret, "/quit"],
    )

    class ProfileApp:
        workspace = tmp_path
        session_id = "configured-thread"
        mode = "build"
        model = "gpt-existing"
        config = Config(
            default_profile="existing",
            profiles={"existing": Profile(name="existing", provider="openai", model="gpt-existing")},
        )

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def command(self, name: str, args: str) -> dict[str, str]:
            self.calls.append((name, args))
            return {"added": "extra"}

    app = ProfileApp()
    result = await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )
    out, err = capsys.readouterr()

    assert result == 0 and err == ""
    assert len(app.calls) == 1
    command_name, command_args = app.calls[0]
    assert command_name in {"model", "config"}
    assert command_args.startswith("add extra openai gpt-test")
    assert secret in command_args
    secret_prompts = [entry for entry in prompts if entry[1]]
    assert len(secret_prompts) == 1
    assert isinstance(secret_prompts[0][2], InMemoryHistory)
    assert secret not in (state / "input_history").read_text(encoding="utf-8")
    assert secret not in out


@pytest.mark.asyncio
async def test_model_add_with_arguments_still_uses_direct_command(tmp_path: Path) -> None:
    class ProfileApp:
        workspace = tmp_path

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def command(self, name: str, args: str) -> dict[str, str]:
            self.calls.append((name, args))
            return {"added": "fast"}

    app = ProfileApp()
    result = await CommandRouter(app, tmp_path, PromptPreferences()).dispatch(
        "/model add fast openai gpt-fast"
    )

    assert app.calls == [("model", "add fast openai gpt-fast")]
    assert "fast" in result.display


@pytest.mark.asyncio
async def test_inline_model_key_is_not_persisted_in_interactive_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SAYACODE_HOME", str(state))
    secret = "sk-inline-history-secret"
    _scripted_prompts(
        monkeypatch,
        [f"/model add fast openai gpt-fast https://models.example/v1 {secret}", "/quit"],
    )

    class ProfileApp:
        workspace = tmp_path
        session_id = "configured-thread"
        mode = "build"
        model = "gpt-existing"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def command(self, name: str, args: str) -> dict[str, str]:
            self.calls.append((name, args))
            return {"added": "fast"}

    app = ProfileApp()
    assert await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    ) == 0
    out, err = capsys.readouterr()

    assert err == ""
    assert app.calls == [
        ("model", f"add fast openai gpt-fast https://models.example/v1 {secret}")
    ]
    assert secret not in (state / "input_history").read_text(encoding="utf-8")
    assert secret not in out


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.asyncio
async def test_help_shows_shortcuts_and_model_add_without_top_session_callout(
    tmp_path: Path, language: str
) -> None:
    router = CommandRouter(object(), tmp_path, PromptPreferences(language=language))
    overview = (await router.dispatch("/help")).display
    model_detail = (await router.dispatch("/help model")).display

    assert "/new" in overview
    assert "/models" in overview
    assert "/model add" in overview + "\n" + model_detail
    # Keep shortcuts in their normal help groups; the new-session action
    # should not receive a separate callout above the command catalog.
    assert "/new" not in overview.splitlines()[1]
    assert "/session new" not in overview.splitlines()[1]
