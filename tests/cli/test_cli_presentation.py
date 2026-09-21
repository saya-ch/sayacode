"""可见命令行展示契约，不锁定具体界面排版。"""

from __future__ import annotations

import json
import re
from argparse import Namespace

import prompt_toolkit
import pytest
from wcwidth import wcswidth

from sayacode.cli.events import _public_event
from sayacode.cli.interactive import _interactive
from sayacode.cli.main import amain
from sayacode.prompts import PromptPreferences

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def test_tool_input_preview_is_visible_but_bounded_and_redacted() -> None:
    public = _public_event(
        {
            "type": "tool.started",
            "tool_name": "write_file",
            "tool_call_id": "call-1",
            "tool_input": {
                "path": "demo.py",
                "content": "x" * 500,
                "api_key": "do-not-show",
            },
        }
    )

    assert public["tool_input"]["path"] == "demo.py"
    assert public["tool_input"]["api_key"] == "***"
    assert len(public["tool_input"]["content"]) <= 241
    assert "do-not-show" not in str(public)


def _fake_prompts(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    remaining = iter(answers)
    labels: list[str] = []

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def prompt_async(self, label: str = "", **kwargs: object) -> str:
            labels.append(str(label))
            try:
                return next(remaining)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)
    return labels


@pytest.mark.parametrize("output_format", ["text", "json", "jsonl"])
@pytest.mark.asyncio
async def test_headless_output_is_machine_clean(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FORCE_COLOR", "1")

    class FakeApp:
        session_id = "thread-1"

        async def run(self, prompt: str, **kwargs: object) -> dict[str, object]:
            return {"ok": True, "status": "completed", "response": "回答完毕"}

        async def stream(self, prompt: str, **kwargs: object):
            yield {"type": "assistant.delta", "delta": "回答完毕"}
            yield {"type": "run.completed", "ok": True, "response": "回答完毕"}

        async def aclose(self) -> None:
            pass

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "你好", "--output-format", output_format],
        app_factory=lambda args: FakeApp(),
    )
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert _ANSI.search(out) is None
    assert "SAYACODE" not in out
    if output_format == "text":
        assert out == "回答完毕\n"
    elif output_format == "json":
        assert json.loads(out)["response"] == "回答完毕"
        assert len(out.splitlines()) == 1
    else:
        events = [json.loads(line) for line in out.splitlines()]
        assert [event["type"] for event in events] == [
            "run.started",
            "assistant.delta",
            "run.completed",
        ]


@pytest.mark.asyncio
async def test_narrow_non_tty_header_shows_active_context(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("COLUMNS", "44")
    monkeypatch.setenv("TERM", "dumb")
    _fake_prompts(monkeypatch, ["/quit"])

    class FakeApp:
        workspace = tmp_path
        session_id = "session-123"
        trust_level = "read_only"
        model = "deepseek-chat"

    result = await _interactive(
        FakeApp(),
        Namespace(workspace=tmp_path, session=None, no_clear=False),
        PromptPreferences(language="zh"),
    )
    out, err = capsys.readouterr()
    assert result == 0 and err == ""
    assert _ANSI.search(out) is None
    assert "SAYACODE" in out
    assert "deepseek-chat" in out
    assert "只读" in out
    assert "session-123" in out
    assert tmp_path.name[:12] in out.replace("\n", "")
    assert max(wcswidth(line) for line in out.splitlines()) <= 44


@pytest.mark.asyncio
async def test_chinese_approval_identifies_each_action_without_leaking_secret(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    labels = _fake_prompts(monkeypatch, ["变更文件", "y", "n"])

    class PausingApp:
        workspace = tmp_path
        session_id = "thread-1"
        trust_level = "ask"
        received: dict[str, object] | None = None

        async def stream(self, prompt: str, **kwargs: object):
            yield {
                "type": "approval.requested",
                "thread_id": "thread-1",
                "action_requests": [
                    {"name": "write_file", "args": {"path": "demo.py", "api_key": "hidden-key"}},
                    {"name": "execute_command_tool", "args": {"command": "git status"}},
                ],
            }
            yield {"type": "run.paused"}

        async def command(self, name: str, args: dict[str, object]) -> dict[str, object]:
            self.received = args
            return {"ok": True, "status": "completed", "response": "处理完毕"}

    app = PausingApp()
    assert (
        await _interactive(
            app,
            Namespace(workspace=tmp_path, session=None, no_clear=True),
            PromptPreferences(language="zh"),
        )
        == 0
    )
    out, _ = capsys.readouterr()
    approval_prompts = [label for label in labels if "批准" in label]
    assert len(approval_prompts) == 2
    assert "write_file" in approval_prompts[0]
    assert "execute_command_tool" in approval_prompts[1]
    assert "1/2" in approval_prompts[0] and "2/2" in approval_prompts[1]
    assert "拒绝" in approval_prompts[0]
    assert "demo.py" in out
    assert "hidden-key" not in out + "".join(labels)
    assert app.received is not None
    assert app.received["decisions"] == [
        {"type": "approve"},
        {"type": "reject", "message": "Declined in terminal"},
    ]


@pytest.mark.asyncio
async def test_interactive_tool_and_task_progress_remains_readable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["检查项目"])

    class ProgressApp:
        workspace = tmp_path
        session_id = "thread-1"
        trust_level = "read_only"
        model = "deepseek-chat"

        async def stream(self, prompt: str, **kwargs: object):
            yield {
                "type": "tool.started",
                "tool_name": "read_file",
                "tool_call_id": "c1",
                "tool_input": {"path": "src/sayacode/cli/display.py"},
            }
            yield {"type": "tool.completed", "tool_name": "read_file", "tool_call_id": "c1"}
            yield {"type": "task.started", "task_id": "task-42", "status": "running"}
            yield {"type": "task.idle", "task_id": "task-42", "status": "idle"}
            yield {"type": "run.completed", "ok": True, "response": "检查完成"}

    assert (
        await _interactive(
            ProgressApp(),
            Namespace(workspace=tmp_path, session=None, no_clear=True),
            PromptPreferences(language="zh"),
        )
        == 0
    )
    out, _ = capsys.readouterr()
    assert "读取文件" in out
    assert "src/sayacode/cli/display.py" in out
    assert "task-42" in out
    assert "检查完成" in out
    assert re.search(r"完成|成功|✓", out)
    assert '{"type"' not in out


@pytest.mark.asyncio
async def test_chinese_failure_has_a_visible_localized_status(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["读取文件"])

    class FailingApp:
        workspace = tmp_path
        session_id = "thread-1"
        trust_level = "read_only"
        model = "deepseek-chat"

        async def stream(self, prompt: str, **kwargs: object):
            yield {"type": "tool.failed", "tool_name": "read_file", "error": "permission denied"}
            yield {"type": "run.failed", "ok": False, "error": "permission denied"}

    assert (
        await _interactive(
            FailingApp(),
            Namespace(workspace=tmp_path, session=None, no_clear=True),
            PromptPreferences(language="zh"),
        )
        == 0
    )
    out, _ = capsys.readouterr()
    assert "读取文件" in out and "permission denied" in out
    assert re.search(r"失败|错误|✗|×", out)
    assert "Tool failed:" not in out


@pytest.mark.asyncio
async def test_autonomous_parent_result_is_visible_in_interactive_terminal(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["/quit"])

    class NotifyingApp:
        workspace = tmp_path
        session_id = "parent-thread"
        trust_level = "ask"
        model = "test-model"

        def watch_notifications(self, callback):
            callback(
                {
                    "type": "agent.wake.completed",
                    "task_id": "child-42",
                    "thread_id": self.session_id,
                    "response": "Parent read the child result",
                }
            )

    assert (
        await _interactive(
            NotifyingApp(),
            Namespace(workspace=tmp_path, session=None, no_clear=True),
            PromptPreferences(language="zh"),
        )
        == 0
    )
    out, _ = capsys.readouterr()
    assert "主 Agent 已根据任务 child-42 继续" in out
    assert "Parent read the child result" in out


@pytest.mark.asyncio
async def test_interactive_can_reject_paused_autonomous_parent_action(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["/reject", "/quit"])

    class PausedApp:
        workspace = tmp_path
        session_id = "parent-thread"
        trust_level = "ask"
        model = "test-model"

        def pending_approval(self, thread_id):
            assert thread_id == self.session_id
            return {
                "thread_id": thread_id,
                "status": "paused",
                "action_requests": [
                    {"name": "execute_command_tool", "args": {"command": "echo no"}}
                ],
            }

        async def command(self, name, args):
            assert name == "reject"
            assert args["decisions"] == [{"type": "reject", "message": "Declined in terminal"}]
            return {"ok": True, "status": "completed", "response": "Rejected safely"}

    assert (
        await _interactive(
            PausedApp(),
            Namespace(workspace=tmp_path, session=None, no_clear=True),
            PromptPreferences(language="zh"),
        )
        == 0
    )
    out, _ = capsys.readouterr()
    assert "execute_command_tool" in out
    assert "Rejected safely" in out


@pytest.mark.asyncio
async def test_headless_jsonl_emits_child_then_autonomous_parent_result(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class CompletedApp:
        session_id = "parent-thread"

        async def stream(self, _prompt: str, **_kwargs: object):
            yield {"type": "run.completed", "ok": True, "response": "Task delegated"}

        async def wait_for_tasks(self):
            return [
                {
                    "task_id": "child-42",
                    "status": "idle",
                    "parent_wake": {
                        "type": "agent.wake.completed",
                        "task_id": "child-42",
                        "thread_id": self.session_id,
                        "response": "Parent reviewed the child",
                    },
                }
            ]

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "delegate", "--output-format", "jsonl"],
        app_factory=lambda _args: CompletedApp(),
    )
    assert code == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["type"] for event in events] == [
        "run.started",
        "task.idle",
        "agent.wake.completed",
        "run.completed",
    ]
    assert "Parent reviewed the child" in events[-1]["response"]


@pytest.mark.asyncio
async def test_streaming_answer_is_not_repeated_by_final_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["简述结果"])

    class StreamingApp:
        workspace = tmp_path
        session_id = "thread-1"
        trust_level = "ask"
        model = "deepseek-chat"

        async def stream(self, prompt: str, **kwargs: object):
            yield {"type": "assistant.delta", "delta": "检查"}
            yield {"type": "assistant.delta", "delta": "完成"}
            yield {"type": "run.completed", "ok": True, "response": "检查完成"}

    assert (
        await _interactive(
            StreamingApp(),
            Namespace(workspace=tmp_path, session=None, no_clear=True),
            PromptPreferences(language="zh"),
        )
        == 0
    )
    out, _ = capsys.readouterr()
    assert out.count("检查完成") == 1


@pytest.mark.asyncio
async def test_status_command_is_rendered_as_readable_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["/status", "/quit"])

    class StatusApp:
        workspace = tmp_path
        session_id = "thread-1"
        trust_level = "read_only"
        model = "deepseek-chat"

        async def command(self, name: str, args: str) -> dict[str, object]:
            assert name == "status"
            return {
                "workspace": str(tmp_path),
                "trust_level": "read_only",
                "model": "deepseek-chat",
                "session_id": "thread-1",
            }

    assert (
        await _interactive(
            StatusApp(),
            Namespace(workspace=tmp_path, session=None, no_clear=True),
            PromptPreferences(language="zh"),
        )
        == 0
    )
    out, _ = capsys.readouterr()
    assert "deepseek-chat" in out and "thread-1" in out and "read_only" in out
    assert '"trust_level":' not in out
    assert '"session_id":' not in out
