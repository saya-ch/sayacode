"""CLI handoff contracts with isolated state and no external model calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import prompt_toolkit
import pytest

from sayacode.app import create_app
from sayacode.cli import _interactive, amain, build_parser
from sayacode.commands import CommandRouter
from sayacode.prompts import PromptPreferences


def _prompt_answers(monkeypatch, answers: list[str]) -> None:
    responses = iter(answers)

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        async def prompt_async(self, *args, **kwargs):
            try:
                return next(responses)
            except StopIteration:
                raise EOFError

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)


@pytest.mark.asyncio
async def test_first_run_without_profile_enters_wizard(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _prompt_answers(
        monkeypatch,
        ["1", "https://models.example.invalid/v1", "", "local-model", "8192", "1024"],
    )
    app = await create_app(build_parser().parse_args(["--workspace", str(tmp_path)]))
    try:
        code = await _interactive(
            app, Namespace(workspace=tmp_path, session=None, no_clear=True), PromptPreferences()
        )
        assert code == 0
        assert app.config.default_profile == "local-model"
        assert app.model == "local-model"
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_session_switch_uses_selected_session_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _prompt_answers(monkeypatch, ["/session use review-thread", "inspect changes"])

    class FakeApp:
        workspace = tmp_path
        session_id = "build-thread"
        mode = "build"
        seen: list[tuple[str, str | None, str | None]] = []

        async def command(self, name, args):
            assert (name, args) == ("session", "use review-thread")
            self.session_id = "review-thread"
            self.mode = "review"
            return {"thread_id": self.session_id, "mode": self.mode}

        async def stream(self, prompt, **kwargs):
            self.seen.append((self.mode, kwargs["mode"], kwargs["session_id"]))
            yield {"type": "run.completed", "response": "reviewed", "ok": True}

    app = FakeApp()
    assert await _interactive(
        app, Namespace(workspace=tmp_path, session=None, no_clear=True), PromptPreferences(mode="build")
    ) == 0
    assert app.seen == [("review", "review", "review-thread")]


@pytest.mark.asyncio
async def test_mixed_approval_grants_only_approved_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _prompt_answers(monkeypatch, ["run tools", "s", "n", "p"])

    class PausingApp:
        workspace = tmp_path
        session_id = "thread-1"
        mode = "build"
        received = None

        async def stream(self, prompt, **kwargs):
            yield {
                "type": "approval.requested", "thread_id": "thread-1",
                "action_requests": [
                    {"name": "write_file"}, {"name": "delete_file"}, {"name": "git"}
                ],
            }
            yield {"type": "run.paused"}

        async def command(self, name, args):
            self.received = (name, args)
            return {"ok": True, "status": "completed", "response": "done"}

    app = PausingApp()
    assert await _interactive(
        app, Namespace(workspace=tmp_path, session=None, no_clear=True), PromptPreferences()
    ) == 0
    assert app.received is not None
    name, payload = app.received
    assert name == "approve"
    assert payload["decisions"] == [
        {"type": "approve"},
        {"type": "reject", "message": "Declined in terminal"},
        {"type": "approve"},
    ]
    assert payload["grants"] == [
        {"index": 0, "scope": "session", "tool_name": "write_file"},
        {"index": 2, "scope": "user", "tool_name": "git"},
    ]


@pytest.mark.parametrize("verb,answers,expected_command,expected_decisions,expected_grants", [
    (
        "approve", ["s", "n"], "approve",
        [{"type": "approve"}, {"type": "reject", "message": "Declined in terminal"}],
        [{"index": 0, "scope": "session", "tool_name": "write_file"}],
    ),
    (
        "reject", [], "reject",
        [{"type": "reject", "message": "Declined in terminal"}] * 2,
        [],
    ),
])
@pytest.mark.asyncio
async def test_team_pending_approval_uses_task_thread(
    tmp_path, monkeypatch, verb, answers, expected_command, expected_decisions, expected_grants
):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _prompt_answers(monkeypatch, [f"/team {verb} task-1", *answers])

    class PendingApp:
        workspace = tmp_path
        session_id = "parent-thread"
        mode = "build"
        resumed = None

        async def command(self, name, args):
            if name == "team":
                assert args == "pending task-1"
                return {
                    "task_id": "task-1", "thread_id": "task-thread", "status": "paused",
                    "action_requests": [{"name": "write_file"}, {"name": "delete_file"}],
                }
            self.resumed = (name, args)
            return {"ok": True, "status": "completed", "response": "resolved"}

    app = PendingApp()
    assert await _interactive(
        app, Namespace(workspace=tmp_path, session=None, no_clear=True), PromptPreferences()
    ) == 0
    assert app.resumed is not None
    command, payload = app.resumed
    assert command == expected_command
    assert payload == {
        "thread_id": "task-thread", "decisions": expected_decisions, "grants": expected_grants
    }


@pytest.mark.asyncio
async def test_headless_team_approval_requires_interactive_input(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))

    class PendingApp:
        session_id = "parent-thread"

        async def command(self, name, args):
            assert (name, args) == ("team", "pending task-1")
            return {
                "task_id": "task-1", "thread_id": "task-thread", "status": "paused",
                "action_requests": [{"name": "write_file"}],
            }

        async def aclose(self):
            pass

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "/team approve task-1",
         "--output-format", "jsonl"],
        app_factory=lambda args: PendingApp(),
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 3
    assert [event["type"] for event in events] == [
        "run.started", "approval.requested", "run.paused"
    ]
    assert events[-1]["thread_id"] == "task-thread"


@pytest.mark.asyncio
async def test_sessions_alias_lists_sessions(tmp_path):
    class FakeApp:
        workspace = tmp_path

        async def command(self, name, args):
            assert (name, args) == ("session", "list")
            return [{"thread_id": "one"}]

    result = await CommandRouter(FakeApp(), tmp_path, PromptPreferences()).dispatch("/sessions")
    assert "one" in result.display


@pytest.mark.asyncio
async def test_mode_and_prefs_report_active_session_mode(tmp_path):
    class FakeApp:
        workspace = tmp_path
        mode = "build"

        async def command(self, name, args):
            assert (name, args) == ("session", "use review-thread")
            self.mode = "review"
            return {"thread_id": "review-thread", "mode": "review"}

    app = FakeApp()
    router = CommandRouter(app, tmp_path, PromptPreferences(mode="build"))
    await router.dispatch("/session use review-thread")
    assert json.loads((await router.dispatch("/prefs")).display)["mode"] == "review"
    assert (await router.dispatch("/mode")).display == "Mode: review"


@pytest.mark.asyncio
async def test_selected_session_restores_its_mode_without_cli_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    first = await create_app(build_parser().parse_args([
        "--workspace", str(tmp_path), "--new-session", "--mode", "review"
    ]))
    session_id = first.session_id
    await first.aclose()
    reopened = await create_app(build_parser().parse_args([
        "--workspace", str(tmp_path), "--session", session_id
    ]))
    try:
        assert reopened.mode == "review"
    finally:
        await reopened.aclose()


@pytest.mark.parametrize("task_status,expected_code,expected_final", [
    ("completed", 0, "run.completed"),
    ("paused", 3, "run.paused"),
    ("failed", 1, "run.failed"),
])
@pytest.mark.asyncio
async def test_jsonl_waits_for_task_before_final_event(
    tmp_path, monkeypatch, capsys, task_status, expected_code, expected_final
):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))

    class FakeApp:
        session_id = "thread-1"
        closed = False
        waited = False

        async def stream(self, prompt, **kwargs):
            yield {"type": "task.started", "task_id": "task-1", "status": "running"}
            yield {"type": "run.completed", "ok": True, "response": "delegated"}

        async def wait_for_tasks(self):
            self.waited = True
            return [{"task_id": "task-1", "status": task_status, "error": None}]

        async def aclose(self):
            assert self.waited
            self.closed = True

    app = FakeApp()
    code = await amain(
        ["--workspace", str(tmp_path), "-p", "delegate", "--output-format", "jsonl"],
        app_factory=lambda args: app,
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == expected_code and app.closed
    assert [item["type"] for item in events] == [
        "run.started", "task.started", f"task.{task_status}", expected_final
    ]


@pytest.mark.asyncio
async def test_headless_paused_uses_exit_code_three(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))

    class FakeApp:
        async def run(self, prompt, **kwargs):
            return {"ok": False, "status": "paused", "thread_id": "thread-1"}

        async def aclose(self):
            pass

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "needs approval", "--output-format", "json"],
        app_factory=lambda args: FakeApp(),
    )
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "paused"


@pytest.mark.asyncio
async def test_jsonl_observed_task_failure_overrides_parent_completion(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))

    class FastTaskApp:
        async def stream(self, prompt, **kwargs):
            yield {"type": "task.failed", "task_id": "fast-task", "status": "failed"}
            yield {"type": "run.completed", "ok": True, "response": "delegated"}

        async def wait_for_tasks(self):
            return []  # A fast child can leave the active-task set before this call.

        async def aclose(self):
            pass

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "delegate", "--output-format", "jsonl"],
        app_factory=lambda args: FastTaskApp(),
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 1
    assert [event["type"] for event in events] == [
        "run.started", "task.failed", "run.failed"
    ]


def test_real_cli_subprocess_returns_failure_without_a_profile(tmp_path):
    environment = os.environ.copy()
    environment["SAYACODE_HOME"] = str(tmp_path / "state")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "sayacode", "--workspace", str(tmp_path),
         "-p", "hello", "--output-format", "json"],
        env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False


def test_real_cli_subprocess_propagates_paused_exit_code(tmp_path):
    environment = os.environ.copy()
    environment["SAYACODE_HOME"] = str(tmp_path / "state")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["TEST_WORKSPACE"] = str(tmp_path)
    script = """
import importlib, os
from sayacode.cli import main
module = importlib.import_module('sayacode.app')
class FakeApp:
    async def run(self, prompt, **kwargs):
        return {'ok': False, 'status': 'paused', 'thread_id': 'task-thread'}
    async def aclose(self):
        pass
async def factory(args):
    return FakeApp()
module.create_app = factory
raise SystemExit(main(['--workspace', os.environ['TEST_WORKSPACE'], '-p', 'approve',
                       '--output-format', 'json']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 3, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "paused" and payload["thread_id"] == "task-thread"
