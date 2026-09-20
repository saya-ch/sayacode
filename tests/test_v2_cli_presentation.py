"""Visible CLI presentation contracts without fixing a particular Rich layout."""

from __future__ import annotations

import json
import re
from argparse import Namespace

import prompt_toolkit
import pytest
from wcwidth import wcswidth

from sayacode.cli import _interactive, amain
from sayacode.prompts import PromptPreferences

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


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
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
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
            "run.started", "assistant.delta", "run.completed"
        ]


@pytest.mark.asyncio
async def test_narrow_non_tty_header_shows_active_context(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("COLUMNS", "44")
    monkeypatch.setenv("TERM", "dumb")
    _fake_prompts(monkeypatch, ["/quit"])

    class FakeApp:
        workspace = tmp_path
        session_id = "session-123"
        mode = "review"
        model = "deepseek-chat"

    result = await _interactive(
        FakeApp(), Namespace(workspace=tmp_path, session=None, no_clear=False),
        PromptPreferences(language="zh", mode="review"),
    )
    out, err = capsys.readouterr()
    assert result == 0 and err == ""
    assert _ANSI.search(out) is None
    assert "SAYACODE" in out
    assert "deepseek-chat" in out
    assert "review" in out
    assert "session-123" in out
    assert tmp_path.name[:12] in out.replace("\n", "")
    assert max(wcswidth(line) for line in out.splitlines()) <= 44


@pytest.mark.asyncio
async def test_chinese_approval_identifies_each_action_without_leaking_secret(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    labels = _fake_prompts(monkeypatch, ["变更文件", "y", "n"])

    class PausingApp:
        workspace = tmp_path
        session_id = "thread-1"
        mode = "build"
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
    assert await _interactive(
        app, Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    ) == 0
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
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["检查项目"])

    class ProgressApp:
        workspace = tmp_path
        session_id = "thread-1"
        mode = "review"
        model = "deepseek-chat"

        async def stream(self, prompt: str, **kwargs: object):
            yield {"type": "tool.started", "tool_name": "read_file", "tool_call_id": "c1"}
            yield {"type": "tool.completed", "tool_name": "read_file", "tool_call_id": "c1"}
            yield {"type": "task.started", "task_id": "task-42", "status": "running"}
            yield {"type": "task.completed", "task_id": "task-42", "status": "completed"}
            yield {"type": "run.completed", "ok": True, "response": "检查完成"}

    assert await _interactive(
        ProgressApp(), Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh", mode="review"),
    ) == 0
    out, _ = capsys.readouterr()
    assert "read_file" in out
    assert "task-42" in out
    assert "检查完成" in out
    assert re.search(r"完成|成功|✓", out)
    assert "{\"type\"" not in out


@pytest.mark.asyncio
async def test_chinese_failure_has_a_visible_localized_status(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["读取文件"])

    class FailingApp:
        workspace = tmp_path
        session_id = "thread-1"
        mode = "review"
        model = "deepseek-chat"

        async def stream(self, prompt: str, **kwargs: object):
            yield {"type": "tool.failed", "tool_name": "read_file", "error": "permission denied"}
            yield {"type": "run.failed", "ok": False, "error": "permission denied"}

    assert await _interactive(
        FailingApp(), Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh", mode="review"),
    ) == 0
    out, _ = capsys.readouterr()
    assert "read_file" in out and "permission denied" in out
    assert re.search(r"失败|错误|✗|×", out)
    assert "Tool failed:" not in out


@pytest.mark.asyncio
async def test_streaming_answer_is_not_repeated_by_final_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["简述结果"])

    class StreamingApp:
        workspace = tmp_path
        session_id = "thread-1"
        mode = "build"
        model = "deepseek-chat"

        async def stream(self, prompt: str, **kwargs: object):
            yield {"type": "assistant.delta", "delta": "检查"}
            yield {"type": "assistant.delta", "delta": "完成"}
            yield {"type": "run.completed", "ok": True, "response": "检查完成"}

    assert await _interactive(
        StreamingApp(), Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh"),
    ) == 0
    out, _ = capsys.readouterr()
    assert out.count("检查完成") == 1


@pytest.mark.asyncio
async def test_status_command_is_rendered_as_readable_fields(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "state"))
    _fake_prompts(monkeypatch, ["/status", "/quit"])

    class StatusApp:
        workspace = tmp_path
        session_id = "thread-1"
        mode = "review"
        model = "deepseek-chat"

        async def command(self, name: str, args: str) -> dict[str, object]:
            assert name == "status"
            return {
                "workspace": str(tmp_path),
                "mode": "review",
                "model": "deepseek-chat",
                "session_id": "thread-1",
            }

    assert await _interactive(
        StatusApp(), Namespace(workspace=tmp_path, session=None, no_clear=True),
        PromptPreferences(language="zh", mode="review"),
    ) == 0
    out, _ = capsys.readouterr()
    assert "deepseek-chat" in out and "thread-1" in out and "review" in out
    assert '"mode":' not in out
    assert '"session_id":' not in out
