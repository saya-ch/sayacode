"""斜杠命令帮助只描述用户实际能调用的命令。"""

from __future__ import annotations

import re
from argparse import Namespace

import prompt_toolkit
import pytest
from wcwidth import wcswidth

from sayacode.cli.commands import BUILTIN_COMMANDS, CommandRouter
from sayacode.cli.interactive import _interactive
from sayacode.prompts import PromptPreferences


def _fake_prompts(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    remaining = iter(answers)

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def prompt_async(self, label: str = "", **kwargs: object) -> str:
            try:
                return next(remaining)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.asyncio
async def test_help_overview_covers_every_invocable_builtin(tmp_path, language: str) -> None:
    router = CommandRouter(object(), PromptPreferences(language=language))
    result = await router.dispatch("/help")

    assert result.prompt is None
    assert result.display
    for name in BUILTIN_COMMANDS:
        assert re.search(rf"/{re.escape(name)}(?![\w-])", result.display), name


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("name", BUILTIN_COMMANDS)
@pytest.mark.asyncio
async def test_each_builtin_has_actionable_help(tmp_path, language: str, name: str) -> None:
    router = CommandRouter(object(), PromptPreferences(language=language))
    result = await router.dispatch(f"/help {name}")

    assert result.prompt is None
    assert re.search(rf"/{re.escape(name)}(?![\w-])", result.display), name
    assert re.search(r"用法|Usage", result.display, flags=re.IGNORECASE), name
    assert re.search(r"示例|Example", result.display, flags=re.IGNORECASE), name


@pytest.mark.parametrize("language,expected", [("zh", "未知"), ("en", "Unknown")])
@pytest.mark.asyncio
async def test_help_unknown_command_gives_discovery_hint(
    tmp_path,
    language: str,
    expected: str,
) -> None:
    router = CommandRouter(object(), PromptPreferences(language=language))
    result = await router.dispatch("/help no_such_command")

    assert expected in result.display
    assert "/help" in result.display
    assert "no_such_command" in result.display


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.asyncio
async def test_session_help_makes_new_session_and_reset_discoverable(
    tmp_path,
    language: str,
) -> None:
    router = CommandRouter(object(), PromptPreferences(language=language))
    overview = (await router.dispatch("/help")).display
    detail = (await router.dispatch("/help session")).display

    assert "/new" in overview
    assert "/reset" in overview
    for action in ("current", "list", "new", "use", "rename"):
        assert re.search(rf"\b{action}\b", detail), action
    assert "/new" in detail


@pytest.mark.parametrize("language,approval_word", [("zh", "审批"), ("en", "approval")])
@pytest.mark.asyncio
async def test_team_help_explains_background_approval(
    tmp_path,
    language: str,
    approval_word: str,
) -> None:
    router = CommandRouter(object(), PromptPreferences(language=language))
    detail = (await router.dispatch("/help team")).display

    assert "/team" in detail
    assert approval_word.lower() in detail.lower()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.asyncio
async def test_help_renders_within_narrow_terminal_and_keeps_requested_detail(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    language: str,
) -> None:
    monkeypatch.setenv("COLUMNS", "44")
    monkeypatch.setenv("TERM", "dumb")
    _fake_prompts(monkeypatch, ["/help", "/help team", "/help no_such_command", "/quit"])

    class FakeApp:
        workspace = tmp_path
        session_id = "help-test"
        trust_level = "read_only"
        model = "test-model"

    code = await _interactive(
        FakeApp(),
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language=language),
    )
    out, err = capsys.readouterr()

    assert code == 0 and err == ""
    assert max(wcswidth(line) for line in out.splitlines()) <= 44
    assert "/team" in out
    assert "/help" in out
    assert re.search(r"用法|Usage", out, flags=re.IGNORECASE)
    assert re.search(r"示例|Example", out, flags=re.IGNORECASE)
    assert ("未知" if language == "zh" else "Unknown") in out
