"""斜杠候选、键盘选择和管道输入的用户行为测试。"""

from __future__ import annotations

import asyncio
from argparse import Namespace
from types import SimpleNamespace

import prompt_toolkit
import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from sayacode.cli.completion import SlashCommandCompleter, slash_command_bindings
from sayacode.cli.interactive import _interactive
from sayacode.cli.selection import choose_option
from sayacode.prompts import PromptPreferences


def _options(completer: SlashCommandCompleter, text: str) -> list[str]:
    return [
        item.text
        for item in completer.get_completions(Document(text), CompleteEvent(text_inserted=True))
    ]


def test_slash_catalog_shows_help_and_fuzzy_subcommands() -> None:
    completer = SlashCommandCompleter(SimpleNamespace(), language=lambda: "zh")
    visible = list(completer.get_completions(Document("/"), CompleteEvent()))
    assert any(item.text == "/help" and "命令" in item.display_meta_text for item in visible)
    assert "/model add" in _options(completer, "/mdad")
    assert not _options(completer, "分析项目")


def test_known_model_skill_and_task_arguments_are_selectable() -> None:
    app = SimpleNamespace(
        config=SimpleNamespace(profiles={"coder": SimpleNamespace(model_id="model-x")}),
        skills=SimpleNamespace(
            list=lambda workspace: [SimpleNamespace(name="review", description="检查代码变更")]
        ),
        workspace="workspace",
        tasks=SimpleNamespace(active_task_ids=lambda: ["task-42"]),
        _spawned_task_ids={"task-43"},
        session_id="session-1",
    )
    completer = SlashCommandCompleter(app, language=lambda: "zh")
    assert "/model use coder" in _options(completer, "/model use c")
    assert "/skill use review" in _options(completer, "/skill use r")
    assert "/team followup task-42" in _options(completer, "/team followup t")
    assert "/team followup task-43" in _options(completer, "/team followup t")
    assert "/session use session-1" in _options(completer, "/session use ")


@pytest.mark.asyncio
async def test_saved_session_and_task_catalog_is_cached_between_keystrokes(tmp_path) -> None:
    calls: list[tuple[str, object]] = []

    class Runtime:
        async def list_threads(self, *, workspace):
            calls.append(("sessions", workspace))
            return [
                {"thread_id": "session-old", "title": "旧项目分析", "status": "completed"},
                {"thread_id": "session-now", "title": "当前工作", "status": "idle"},
                {"thread_id": "task-thread", "title": "内部任务", "is_background": True},
            ]

    class Tasks:
        async def list(self, *, workspace):
            calls.append(("tasks", workspace))
            return [
                SimpleNamespace(
                    task_id="task-paused",
                    title="修复解析器",
                    role="builder",
                    status="paused",
                )
            ]

    app = SimpleNamespace(
        workspace=tmp_path,
        session_id="session-now",
        runtime=Runtime(),
        tasks=Tasks(),
    )
    completer = SlashCommandCompleter(app, language=lambda: "zh")
    await completer.refresh_directory()

    sessions = list(completer.get_completions(Document("/session use "), CompleteEvent()))
    tasks = list(completer.get_completions(Document("/team approve "), CompleteEvent()))
    assert "/session use session-old" in [item.text for item in sessions]
    assert "旧项目分析" in next(
        item.display_meta_text for item in sessions if item.text.endswith("session-old")
    )
    assert "/session use task-thread" not in [item.text for item in sessions]
    assert [item.text for item in tasks] == ["/team approve task-paused"]
    assert "修复解析器" in tasks[0].display_meta_text
    for _ in range(3):
        _options(completer, "/session use s")
        _options(completer, "/team approve t")
    assert calls == [("sessions", tmp_path), ("tasks", tmp_path)]


@pytest.mark.asyncio
async def test_interactive_refreshes_saved_catalog_before_each_main_input(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    offered: list[list[str]] = []

    class Runtime:
        calls = 0

        async def list_threads(self, *, workspace):
            self.calls += 1
            return [{"thread_id": f"session-{self.calls}", "title": f"会话 {self.calls}"}]

    class Tasks:
        calls = 0

        async def list(self, *, workspace):
            self.calls += 1
            return [
                SimpleNamespace(
                    task_id=f"task-{self.calls}",
                    title=f"任务 {self.calls}",
                    role="builder",
                    status="paused",
                )
            ]

    class FakeSession:
        def __init__(self, **kwargs):
            self.completer = kwargs["completer"]

        async def prompt_async(self, label: str = "", **kwargs) -> str:
            offered.append(
                [
                    *_options(self.completer, "/session use "),
                    *_options(self.completer, "/team approve "),
                ]
            )
            return "/help" if len(offered) == 1 else "/quit"

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)
    app = SimpleNamespace(
        workspace=tmp_path,
        session_id="session-current",
        trust_level="ask",
        model="test-model",
        runtime=Runtime(),
        tasks=Tasks(),
    )
    code = await _interactive(
        app,
        Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    )
    assert code == 0
    assert app.runtime.calls == app.tasks.calls == 2
    assert "/session use session-1" in offered[0]
    assert "/team approve task-1" in offered[0]
    assert "/session use session-2" in offered[1]
    assert "/team approve task-2" in offered[1]
    assert "/session use session-1" not in offered[1]


@pytest.mark.asyncio
async def test_slash_menu_uses_down_up_and_enter_before_submitting() -> None:
    with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
        completer = SlashCommandCompleter(SimpleNamespace(), language=lambda: "zh")
        first = _options(completer, "/")[0]
        session: PromptSession[str] = PromptSession(
            completer=completer,
            key_bindings=slash_command_bindings(),
            complete_while_typing=True,
        )
        answer = asyncio.create_task(session.prompt_async("❯ "))
        await asyncio.sleep(0.05)
        source.send_text("/")
        await asyncio.sleep(0.1)
        assert session.default_buffer.complete_state is not None
        source.send_text("\x1b[B\x1b[B\x1b[A\r")
        await asyncio.sleep(0.1)
        assert not answer.done()
        assert session.default_buffer.text == first
        source.send_text("\r")
        assert await asyncio.wait_for(answer, 2) == first


@pytest.mark.asyncio
async def test_up_without_command_menu_still_recalls_history() -> None:
    history = InMemoryHistory()
    history.append_string("之前的任务")
    with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
        session: PromptSession[str] = PromptSession(
            history=history,
            completer=SlashCommandCompleter(SimpleNamespace(), language=lambda: "zh"),
            key_bindings=slash_command_bindings(),
        )
        answer = asyncio.create_task(session.prompt_async("❯ "))
        await asyncio.sleep(0.05)
        source.send_text("\x1b[A\r")
        assert await asyncio.wait_for(answer, 2) == "之前的任务"


@pytest.mark.asyncio
async def test_protocol_menu_selects_second_item_with_arrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sayacode.cli.selection._is_interactive_terminal", lambda: True)
    with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
        task = asyncio.create_task(
            choose_option(
                object(),
                label="选择协议",
                fallback_label="协议：",
                options=(("chat", "Chat Completions"), ("responses", "Responses API")),
                invalid_message="请重选",
                notice=lambda message: None,
                language="zh",
            )
        )
        await asyncio.sleep(0.05)
        source.send_text("\x1b[B\r")
        assert await asyncio.wait_for(task, 2) == "responses"
