"""跨会话记忆的终端操作与人工说明职责测试。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from wcwidth import wcswidth

from sayacode.cli.completion import SlashCommandCompleter
from sayacode.cli.display import TerminalPresenter
from sayacode.cli.events import _public_event
from sayacode.cli.help import format_help
from sayacode.cli.interactive import _create_prompt_session, _install_notification_watcher
from sayacode.cli.memory import dispatch_memory_command, run_memory_menu
from sayacode.cli.result_views import CommandResultRenderer
from sayacode.config import Config, MemoryConfig
from sayacode.extensions.instructions import load_project_instructions
from sayacode.paths import AppPaths


class FakeMemory:
    def __init__(self) -> None:
        self.records = [
            {"id": "mem-1", "subject": "中文注释", "scope": "user", "state": "active", "text": "代码注释用中文", "pinned": False}
        ]
        self.config = {
            "enabled": False,
            "use": True,
            "learn": "auto",
            "model_profile": None,
            "headless_timeout_seconds": 30.0,
        }
        self.session_overrides: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    async def status(self) -> dict[str, Any]:
        return self.config.copy()

    async def settings(self, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("settings", updates))
        self.config.update(updates or {})
        return self.config.copy()

    async def session_settings(
        self, thread_id: str, updates: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("session_settings", (thread_id, updates)))
        current = self.session_overrides.setdefault(thread_id, {"use": None, "learn": None})
        current.update(updates or {})
        return {
            "thread_id": thread_id,
            "use_override": current["use"],
            "learn_override": current["learn"],
            "use": self.config["enabled"]
            and (self.config["use"] if current["use"] is None else current["use"]),
            "learn": self.config["learn"] if current["learn"] is None else current["learn"],
        }

    async def list(self, *, scope: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list", (scope, query)))
        return [
            item
            for item in self.records
            if (scope is None or item["scope"] == scope)
            and (query is None or query in item["text"])
        ]

    async def recent(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("recent", thread_id))
        return [
            {
                **item,
                "match_reason": "与当前任务有关",
                "provided_to_model": True,
            }
            for item in self.records
        ]

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        self.calls.append(("get", memory_id))
        record = next((item for item in self.records if item["id"] == memory_id), None)
        return {
            **record,
            "evidence": [
                {
                    "source_ref": "turn-42",
                    "message_id": "user-1",
                    "role": "user",
                    "preview": "以后代码注释用中文",
                }
            ],
        } if record is not None else None

    async def remember(self, text: str, *, scope: str) -> dict[str, Any]:
        self.calls.append(("remember", (scope, text)))
        return {"id": "mem-2", "subject": text[:20], "text": text, "scope": scope}

    async def correct(self, memory_id: str, text: str) -> dict[str, Any]:
        self.calls.append(("correct", (memory_id, text)))
        return {"id": memory_id, "text": text}

    async def confirm(self, memory_id: str) -> dict[str, Any]:
        self.calls.append(("confirm", memory_id))
        return {"id": memory_id, "state": "active"}

    async def pin(self, memory_id: str, *, pinned: bool) -> dict[str, Any]:
        self.calls.append(("pin", (memory_id, pinned)))
        record = next(item for item in self.records if item["id"] == memory_id)
        record["pinned"] = pinned
        return record.copy()

    async def forget(self, memory_id: str) -> dict[str, Any]:
        self.calls.append(("forget", memory_id))
        self.records = [item for item in self.records if item["id"] != memory_id]
        return {"forgotten": memory_id}

    async def refresh(self, memory_id: str | None) -> dict[str, Any]:
        self.calls.append(("refresh", memory_id))
        return {"refreshed": memory_id}


@pytest.mark.asyncio
async def test_memory_commands_manage_one_store_facade_and_reject_old_file_actions() -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory)
    assert (await dispatch_memory_command(app, "status"))["enabled"] is False
    assert (await dispatch_memory_command(app, "enable"))["enabled"] is True
    assert (await dispatch_memory_command(app, "use off"))["use"] is False
    assert (await dispatch_memory_command(app, "learn explicit"))["learn"] == "explicit"
    assert (await dispatch_memory_command(app, "model coder"))["model_profile"] == "coder"
    assert (await dispatch_memory_command(app, "model 'coder plus'"))["model_profile"] == "coder plus"
    assert (await dispatch_memory_command(app, "model default"))["model_profile"] is None
    assert (await dispatch_memory_command(app, "timeout 20"))["headless_timeout_seconds"] == 20.0
    assert (await dispatch_memory_command(app, "search 中文"))[0]["id"] == "mem-1"
    assert (await dispatch_memory_command(app, "show mem-1"))["text"] == "代码注释用中文"
    assert (await dispatch_memory_command(app, 'remember project "依赖由 uv 管理"'))["scope"] == "project"
    assert (await dispatch_memory_command(app, 'correct mem-1 "注释改用英文"'))["text"] == "注释改用英文"
    assert (await dispatch_memory_command(app, "confirm mem-1"))["state"] == "active"
    assert (await dispatch_memory_command(app, "pin mem-1"))["pinned"] is True
    assert (await dispatch_memory_command(app, "unpin mem-1"))["pinned"] is False
    assert (await dispatch_memory_command(app, "refresh mem-1"))["refreshed"] == "mem-1"
    assert (await dispatch_memory_command(app, "forget mem-1"))["forgotten"] == "mem-1"
    assert (await dispatch_memory_command(app, "list")) == []
    with pytest.raises(ValueError, match="Usage"):
        await dispatch_memory_command(app, "append user old-file-text")
    with pytest.raises(ValueError, match="Usage"):
        await dispatch_memory_command(app, "init project")
    with pytest.raises(ValueError, match="Usage"):
        await dispatch_memory_command(app, "pin")
    with pytest.raises(ValueError, match="Usage"):
        await dispatch_memory_command(app, "timeout soon")


@pytest.mark.asyncio
async def test_memory_commands_keep_unquoted_windows_path_text() -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory)
    path = r"C:\develop\sayacode\README.md"
    await dispatch_memory_command(app, f"remember project 说明位于 {path}")
    await dispatch_memory_command(app, f"correct mem-1 改用 {path}")
    assert ("remember", ("project", f"说明位于 {path}")) in memory.calls
    assert ("correct", ("mem-1", f"改用 {path}")) in memory.calls


@pytest.mark.asyncio
async def test_session_memory_settings_override_and_restore_global_defaults() -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory, session_id="session-a")
    await dispatch_memory_command(app, "enable")
    baseline = await dispatch_memory_command(app, "session")
    assert baseline["use"] is True and baseline["use_override"] is None
    changed = await dispatch_memory_command(app, "session use off")
    assert changed["use"] is False and changed["use_override"] is False
    changed = await dispatch_memory_command(app, "session learn explicit")
    assert changed["learn"] == "explicit" and changed["learn_override"] == "explicit"
    other = await memory.session_settings("session-b")
    assert other["use"] is True and other["learn"] == "auto"
    restored = await dispatch_memory_command(app, "session use default")
    assert restored["use"] is True and restored["use_override"] is None
    restored = await dispatch_memory_command(app, "session learn default")
    assert restored["learn"] == "auto" and restored["learn_override"] is None
    with pytest.raises(ValueError, match="Usage"):
        await dispatch_memory_command(app, "session use maybe")
    with pytest.raises(ValueError, match="Usage"):
        await dispatch_memory_command(app, "session learn maybe")


@pytest.mark.asyncio
async def test_recent_memory_command_reports_provided_records_for_current_session() -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory, session_id="session-a")
    recent = await dispatch_memory_command(app, "recent")
    assert recent[0]["provided_to_model"] is True
    assert recent[0]["match_reason"] == "与当前任务有关"
    assert ("recent", "session-a") in memory.calls
    with pytest.raises(ValueError, match="Usage"):
        await dispatch_memory_command(app, "recent extra")


@pytest.mark.asyncio
async def test_memory_menu_selects_and_forgets_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory)
    choices = deque(["list", "mem-1", "forget", True, "back"])
    shown: list[str] = []

    async def choose(*args: Any, **kwargs: Any) -> Any:
        return choices.popleft()

    monkeypatch.setattr("sayacode.cli.memory._is_interactive_terminal", lambda: True)
    monkeypatch.setattr("sayacode.cli.memory.choose_option", choose)
    presenter = SimpleNamespace(
        command_result=lambda command, value: shown.append(command),
        notice=lambda message: None,
    )
    assert await run_memory_menu(app, object(), presenter, "zh")
    assert ("forget", "mem-1") in memory.calls
    assert "/memory show" in shown and "/memory forget" in shown
    assert not choices


@pytest.mark.asyncio
async def test_memory_menu_toggles_pin_for_selected_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory)
    choices = deque(["list", "mem-1", "pin", "list", "mem-1", "unpin", "back"])
    shown: list[str] = []

    async def choose(*args: Any, **kwargs: Any) -> Any:
        return choices.popleft()

    monkeypatch.setattr("sayacode.cli.memory._is_interactive_terminal", lambda: True)
    monkeypatch.setattr("sayacode.cli.memory.choose_option", choose)
    presenter = SimpleNamespace(
        command_result=lambda command, value: shown.append(command),
        notice=lambda message: None,
    )
    assert await run_memory_menu(app, object(), presenter, "zh")
    assert ("pin", ("mem-1", True)) in memory.calls
    assert ("pin", ("mem-1", False)) in memory.calls
    assert memory.records[0]["pinned"] is False
    assert "/memory pin" in shown and "/memory unpin" in shown


@pytest.mark.asyncio
async def test_memory_menu_sets_only_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory, session_id="session-a")
    choices = deque(["session", "session use off", "back"])
    shown: list[str] = []

    async def choose(*args: Any, **kwargs: Any) -> Any:
        return choices.popleft()

    monkeypatch.setattr("sayacode.cli.memory._is_interactive_terminal", lambda: True)
    monkeypatch.setattr("sayacode.cli.memory.choose_option", choose)
    presenter = SimpleNamespace(
        command_result=lambda command, value: shown.append(command),
        notice=lambda message: None,
    )
    assert await run_memory_menu(app, object(), presenter, "zh")
    assert ("session_settings", ("session-a", {"use": False})) in memory.calls
    assert memory.config["use"] is True
    assert "/memory session" in shown


@pytest.mark.asyncio
async def test_memory_menu_opens_recent_record_without_claiming_it_was_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeMemory()
    app = SimpleNamespace(memory=memory, session_id="session-a")
    choices = deque(["recent", "mem-1", "back", "back"])
    shown: list[str] = []

    async def choose(*args: Any, **kwargs: Any) -> Any:
        return choices.popleft()

    monkeypatch.setattr("sayacode.cli.memory._is_interactive_terminal", lambda: True)
    monkeypatch.setattr("sayacode.cli.memory.choose_option", choose)
    presenter = SimpleNamespace(
        command_result=lambda command, value: shown.append(command),
        notice=lambda message: None,
    )
    assert await run_memory_menu(app, object(), presenter, "zh")
    assert ("recent", "session-a") in memory.calls
    assert "/memory recent" in shown and "/memory show" in shown


@pytest.mark.asyncio
async def test_memory_menu_selects_saved_model_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeMemory()
    app = SimpleNamespace(
        memory=memory,
        config=SimpleNamespace(
            profiles={
                "coder": SimpleNamespace(model_id="fast-coder"),
                "coder plus": SimpleNamespace(model_id="large-coder"),
            }
        ),
    )
    choices = deque(["settings", "model", "coder", "back"])
    menus: list[list[str]] = []

    async def choose(*args: Any, **kwargs: Any) -> Any:
        menus.append([value for value, _ in kwargs["options"]])
        return choices.popleft()

    monkeypatch.setattr("sayacode.cli.memory._is_interactive_terminal", lambda: True)
    monkeypatch.setattr("sayacode.cli.memory.choose_option", choose)
    presenter = SimpleNamespace(command_result=lambda *args: None, notice=lambda message: None)
    assert await run_memory_menu(app, object(), presenter, "zh")
    assert memory.config["model_profile"] == "coder"
    assert ["default", "coder", "coder plus"] in menus


@pytest.mark.asyncio
async def test_memory_ids_appear_in_slash_menu() -> None:
    memory = FakeMemory()
    app = SimpleNamespace(
        memory=memory,
        config=SimpleNamespace(
            profiles={
                "coder": SimpleNamespace(model_id="fast-coder"),
                "coder plus": SimpleNamespace(model_id="large-coder"),
            }
        ),
    )
    completer = SlashCommandCompleter(app, language=lambda: "zh")
    await completer.refresh_directory()
    candidates = [
        item.text
        for item in completer.get_completions(
            Document("/memory forget mem"), CompleteEvent(text_inserted=True)
        )
    ]
    assert "/memory forget mem-1" in candidates
    for action in ("pin", "unpin"):
        options = [
            item.text
            for item in completer.get_completions(
                Document(f"/memory {action} mem"), CompleteEvent(text_inserted=True)
            )
        ]
        assert f"/memory {action} mem-1" in options
    assert "pin <ID>" in format_help("memory", language="en")
    assert "unpin <ID>" in format_help("memory", language="en")
    for typed, expected in (
        ("/memory session use d", "/memory session use default"),
        ("/memory session learn e", "/memory session learn explicit"),
        ("/memory use o", "/memory use on"),
    ):
        options = [
            item.text
            for item in completer.get_completions(Document(typed), CompleteEvent(text_inserted=True))
        ]
        assert expected in options
    assert "session use <on|off|default>" in format_help("memory", language="en")
    assert "/memory recent" in [
        item.text
        for item in completer.get_completions(Document("/memory rec"), CompleteEvent())
    ]
    assert "/memory model coder" in [
        item.text
        for item in completer.get_completions(Document("/memory model c"), CompleteEvent())
    ]
    assert "/memory model default" in [
        item.text
        for item in completer.get_completions(Document("/memory model d"), CompleteEvent())
    ]
    assert "/memory model coder plus" in [
        item.text
        for item in completer.get_completions(
            Document("/memory model coder p"), CompleteEvent()
        )
    ]


def test_memory_config_defaults_closed_and_roundtrips() -> None:
    config = Config.from_dict({})
    assert config.memory == MemoryConfig()
    assert config.memory.enabled is False
    assert Config.from_dict(config.to_dict()).memory == config.memory
    with pytest.raises(ValueError, match="memory.learn"):
        MemoryConfig(learn="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="memory.learn"):
        Config.from_dict({"memory": {"learn": []}})
    with pytest.raises(ValueError, match="unknown memory fields"):
        Config.from_dict({"memory": {"mdoel_profile": "typo"}})


def test_manual_instructions_have_a_separate_path_and_ignore_old_memory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    paths = AppPaths.resolve(home)
    assert paths.instructions == home / "instructions.md"
    assert not hasattr(paths, "memory")
    paths.instructions.write_text("用户手工说明", encoding="utf-8")
    (home / "memory.md").write_text("不再加载旧文件", encoding="utf-8")
    (workspace / "SAYACODE.md").write_text("项目约定", encoding="utf-8")
    old_project_memory = workspace / ".sayacode" / "memory.md"
    old_project_memory.parent.mkdir()
    old_project_memory.write_text("不再加载旧项目记忆", encoding="utf-8")
    instructions = load_project_instructions(workspace, paths.instructions)
    assert "用户手工说明" in instructions and "项目约定" in instructions
    assert "不再加载旧文件" not in instructions
    assert "不再加载旧项目记忆" not in instructions


def test_memory_results_show_status_candidates_and_source_references() -> None:
    output = io.StringIO()
    console = Console(file=output, width=92, force_terminal=False)
    renderer = CommandResultRenderer(
        console,
        is_chinese=lambda: True,
        redact=lambda value: value,
        notice=lambda message, **kwargs: output.write(message + "\n"),
    )
    renderer.render(
        "/memory status",
        json.dumps(
            {
                "enabled": True,
                "use": True,
                "learn": "auto",
                "counts": {"active": 1, "candidate": 2, "needs_verification": 3},
                "settings": {
                    "enabled": True,
                    "use": True,
                    "learn": "auto",
                    "model_profile": None,
                    "headless_timeout_seconds": 30.0,
                },
            }
        ),
    )
    renderer.render(
        "/memory list",
        json.dumps(
            [
                {
                    "id": "mem-2",
                    "subject": "项目测试约定",
                    "scope": {"kind": "project"},
                    "state": "candidate",
                    "pinned": True,
                }
            ],
            ensure_ascii=False,
        ),
    )
    renderer.render(
        "/memory recent",
        json.dumps(
            [
                {
                    "id": "mem-2",
                    "subject": "项目测试约定",
                    "scope": {"kind": "project"},
                    "state": "active",
                    "match_reason": "当前任务涉及测试",
                    "provided_to_model": True,
                }
            ],
            ensure_ascii=False,
        ),
    )
    renderer.render(
        "/memory show mem-2",
        json.dumps(
            {
                "id": "mem-2",
                "subject": "项目测试约定",
                "text": "测试用 uv run pytest",
                "scope": {"kind": "project"},
                "state": "candidate",
                "pinned": True,
                "sources": [{"kind": "user", "ref": "turn-42"}],
                "evidence": [
                    {
                        "source_ref": "turn-42",
                        "message_id": "user-1",
                        "role": "user",
                        "preview": "项目统一用 uv 运行测试",
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )
    rendered = output.getvalue()
    assert "候选" in rendered and "待核验" in rendered
    assert "mem-2" in rendered and "测试用 uv run pytest" in rendered
    assert "turn-42" in rendered
    assert "固定" in rendered
    assert "当前任务涉及测试" in rendered
    assert "已提供不代表实际采用" in rendered
    assert "项目统一用 uv 运行测试" in rendered


def test_memory_session_result_shows_effective_value_and_override_source() -> None:
    output = io.StringIO()
    renderer = CommandResultRenderer(
        Console(file=output, width=90, force_terminal=False),
        is_chinese=lambda: True,
        redact=lambda value: value,
        notice=lambda message, **kwargs: output.write(message + "\n"),
    )
    renderer.render(
        "/memory session",
        json.dumps(
            {
                "thread_id": "session-a",
                "use_override": False,
                "learn_override": None,
                "use": False,
                "learn": "auto",
            }
        ),
    )
    shown = output.getvalue()
    assert "当前会话记忆设置" in shown
    assert "关 · 当前会话覆盖" in shown
    assert "auto · 继承全局" in shown


def test_recent_memory_result_remains_readable_in_narrow_terminal() -> None:
    output = io.StringIO()
    renderer = CommandResultRenderer(
        Console(file=output, width=44, force_terminal=False),
        is_chinese=lambda: True,
        redact=lambda value: value,
        notice=lambda message, **kwargs: output.write(message + "\n"),
    )
    renderer.render(
        "/memory recent",
        json.dumps(
            [
                {
                    "id": "mem-1",
                    "subject": "注释语言",
                    "scope": {"kind": "user"},
                    "state": "active",
                    "match_reason": "当前任务提到了代码注释",
                    "provided_to_model": True,
                }
            ],
            ensure_ascii=False,
        ),
    )
    lines = output.getvalue().splitlines()
    assert max(wcswidth(line) for line in lines) <= 44
    assert "命中" in output.getvalue()


def test_public_memory_events_only_contain_reference_and_status_fields() -> None:
    public = _public_event(
        {
            "type": "memory.updated",
            "thread_id": "session-1",
            "count": 2,
            "source_ref": "turn-42",
            "memory_text": "PRIVATE_CONTENT",
            "model_input": "PRIVATE_CONTEXT",
        }
    )
    assert public == {
        "type": "memory.updated",
        "thread_id": "session-1",
        "count": 2,
        "source_ref": "turn-42",
    }


def test_memory_text_commands_do_not_enter_persistent_input_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path))
    app = SimpleNamespace(session_id="session-1", trust_level="ask", model="test")
    presenter = SimpleNamespace(zh=True, task_count=lambda: 0, todo_progress=lambda: (0, 0))
    completer = SlashCommandCompleter(app, language=lambda: "zh")
    with create_app_session(output=DummyOutput()):
        session = _create_prompt_session(app, presenter, completer)
        session.history.append_string("/memory remember user 私人偏好")
        session.history.append_string("/memory correct mem-1 新正文")
        session.history.append_string("/memory list")
    saved = (tmp_path / "input_history").read_text(encoding="utf-8")
    assert "/memory list" in saved
    assert "私人偏好" not in saved and "新正文" not in saved


@pytest.mark.asyncio
async def test_memory_notice_does_not_change_subagent_count_and_toolbar_tracks_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path))
    output = io.StringIO()
    presenter = TerminalPresenter(
        Console(file=output, width=90, force_terminal=False),
        language="zh",
        redact=lambda value: value,
    )

    class MemoryStatus:
        async def status(self) -> dict[str, int]:
            return {"active_jobs": 1, "pending": 2}

    class FakeApp:
        session_id = "session-1"
        trust_level = "ask"
        model = "test"
        memory = MemoryStatus()
        callback: Any = None

        def watch_notifications(self, callback: Any) -> None:
            self.callback = callback

    app = FakeApp()
    completer = SlashCommandCompleter(app, language=lambda: "zh")
    with create_app_session(output=DummyOutput()):
        session = _create_prompt_session(app, presenter, completer)
        _install_notification_watcher(app, session, presenter)
        outcome = app.callback({"type": "memory.updated", "thread_id": "session-1", "count": 1})
        if outcome is not None:
            await outcome
        toolbar = fragment_list_to_text(to_formatted_text(session.bottom_toolbar()))
        active_progress = presenter.memory_progress()
        presenter.update_memory_status({"active_jobs": 0, "pending": 3})
        pending_toolbar = fragment_list_to_text(to_formatted_text(session.bottom_toolbar()))

    assert presenter.task_count() == 0
    assert active_progress == (1, 2)
    assert presenter.memory_progress() == (0, 3)
    assert "记忆整理中 1 / 待处理 2" in toolbar
    assert "记忆待处理 3" in pending_toolbar
    assert "记忆更新 1 项" in output.getvalue()
    assert "任务 ?" not in output.getvalue()


@pytest.mark.parametrize("fail_flush", [False, True])
@pytest.mark.parametrize("no_stream", [False, True])
def test_headless_subprocess_places_redacted_memory_event_before_terminal_event(
    tmp_path: Path, fail_flush: bool, no_stream: bool
) -> None:
    """真实 CLI 进程的记忆收尾不改变主任务结果，也不泄露正文。"""
    script = r'''
import os
from sayacode.cli.main import main
import sayacode.application as application

class FakeMemory:
    async def flush_headless(self, thread_id):
        app.notifications.append({
            "type": "memory.updated", "thread_id": "another-session",
            "source_ref": "other-turn", "count": 1,
        })
        if os.environ.get("TEST_FLUSH_FAIL") == "1":
            raise RuntimeError("private model details")
        app.notifications.append({
            "type": "memory.updated", "thread_id": thread_id,
            "source_ref": "turn-42", "count": 1,
            "memory_text": "SECRET_MUST_NOT_OUTPUT",
        })

class FakeApp:
    session_id = "session-1"
    def __init__(self):
        self.notifications = []
        self.memory = FakeMemory()
    async def run(self, prompt, **kwargs):
        return {"ok": True, "status": "completed", "thread_id": "session-1", "response": "done"}
    async def stream(self, prompt, **kwargs):
        yield {"type": "memory.updated", "thread_id": "another-session", "memory_text": "SECRET_MUST_NOT_OUTPUT"}
        yield {"type": "assistant.delta", "delta": "done", "thread_id": "session-1"}
        yield {"type": "run.completed", "thread_id": "session-1", "response": "done", "ok": True}
    def drain_notifications(self):
        events = self.notifications[:]
        self.notifications.clear()
        return events
    async def aclose(self):
        pass

app = FakeApp()
async def create_app(args):
    return app
application.create_app = create_app
arguments = [
    "--workspace", os.environ["TEST_WORKSPACE"],
    "-p", "任务", "--output-format", "jsonl",
]
if os.environ.get("TEST_NO_STREAM") == "1":
    arguments.append("--no-stream")
raise SystemExit(main(arguments))
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    environment["SAYACODE_HOME"] = str(tmp_path / "home")
    environment["TEST_WORKSPACE"] = str(tmp_path)
    environment["TEST_FLUSH_FAIL"] = "1" if fail_flush else "0"
    environment["TEST_NO_STREAM"] = "1" if no_stream else "0"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    expected = ["run.started"]
    if not no_stream:
        expected.append("assistant.delta")
    expected.extend(["memory.failed" if fail_flush else "memory.updated", "run.completed"])
    assert [event["type"] for event in events] == expected
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert "SECRET_MUST_NOT_OUTPUT" not in result.stdout
    assert "private model details" not in result.stdout
    assert events[-1]["response"] == "done"
