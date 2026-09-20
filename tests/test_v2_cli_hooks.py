"""Focused contracts for the new CLI protocol and command hooks."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import prompt_toolkit
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sayacode.cli import _interactive, amain  # noqa: E402
from sayacode.custom_commands import discover_custom_commands, expand_custom_command  # noqa: E402
from sayacode.hooks import HookRuntime  # noqa: E402
from sayacode.prompts import PromptPreferences  # noqa: E402


class _FakeApp:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.session_id = "test-thread"
        self.closed = False

    async def stream(self, prompt: str, **kwargs):
        assert prompt == "hello"
        assert kwargs["input_format"] == "headless"
        yield {"type": "assistant.delta", "delta": "hello"}
        yield {
            "type": "tool.started",
            "tool_name": "read_file",
            "tool_call_id": "call-1",
            "args": {"api_key": "very-secret"},
        }
        yield {"type": "run.completed", "ok": True, "response": "hello"}

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_jsonl_stream_is_numbered_and_hides_tool_inputs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    app = _FakeApp(tmp_path)
    code = await amain(
        ["--workspace", str(tmp_path), "-p", "hello", "--output-format", "jsonl"],
        app_factory=lambda args: app,
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0 and app.closed
    assert [event["type"] for event in events] == [
        "run.started",
        "assistant.delta",
        "tool.started",
        "run.completed",
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["run_id"] == events[0]["run_id"] for event in events)
    assert "very-secret" not in json.dumps(events)
    assert "args" not in events[2]


@pytest.mark.asyncio
async def test_interactive_approval_resumes_only_after_stream_closes(tmp_path, monkeypatch):
    answers = iter(["change file", "y"])

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        async def prompt_async(self, *args, **kwargs):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)

    class PausingApp:
        workspace = tmp_path
        session_id = "thread-1"
        stream_closed = False
        decision = None

        async def stream(self, prompt, **kwargs):
            assert prompt == "change file"
            try:
                yield {
                    "type": "approval.requested",
                    "thread_id": "thread-1",
                    "action_requests": [{"name": "write_file"}],
                }
                yield {"type": "run.paused"}
            finally:
                self.stream_closed = True

        async def command(self, name, args):
            assert self.stream_closed
            self.decision = (name, args)
            return {"ok": True, "status": "completed", "response": "done"}

    app = PausingApp()
    code = await _interactive(
        app, Namespace(workspace=tmp_path, session=None, no_clear=True), PromptPreferences()
    )
    assert code == 0
    assert app.decision is not None
    name, payload = app.decision
    assert name == "approve"
    assert payload["thread_id"] == "thread-1"
    assert payload["decisions"] == [{"type": "approve"}]
    assert payload["grants"] == []


def test_markdown_command_expansion_prefers_project_and_replaces_positions(tmp_path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    (project / ".claude" / "commands").mkdir(parents=True)
    (user / ".claude" / "commands").mkdir(parents=True)
    (project / ".claude" / "commands" / "review.md").write_text(
        "---\ndescription: Review code\n---\nReview $1, then $ARGUMENTS and $2.",
        encoding="utf-8",
    )
    (user / ".claude" / "commands" / "review.md").write_text("User command", encoding="utf-8")
    commands = discover_custom_commands(project, home=user)
    expanded = expand_custom_command('/review "src code" tests', commands)
    assert expanded is not None
    command, prompt = expanded
    assert command.scope == "project"
    assert prompt == 'Review src code, then "src code" tests and tests.'


@pytest.mark.asyncio
async def test_project_hook_needs_trust_and_blocks_after_trust(tmp_path):
    workspace = tmp_path / "project"
    config = workspace / ".sayacode" / "hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "command": [sys.executable, "-c", "import sys; sys.exit(7)"],
                            "blocking": True,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    hooks = HookRuntime(workspace, state_home=tmp_path / "state")
    assert hooks.status()["project_hooks"] == 0
    assert await hooks.trigger("PreToolUse", {"tool": "write_file"}) is None
    hooks.trust()
    assert hooks.status()["project_hooks"] == 1
    blocked = await hooks.trigger("PreToolUse", {"tool": "write_file"})
    assert blocked and "blocked PreToolUse" in blocked
    hooks.untrust()
    assert hooks.status()["project_hooks"] == 0
