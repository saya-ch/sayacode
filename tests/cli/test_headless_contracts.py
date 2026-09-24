"""无头 CLI 的输出、退出码和隐私边界。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sayacode.cli.events import _public_event
from sayacode.cli.main import amain, build_parser


@pytest.mark.parametrize("output_format", ["text", "json", "jsonl"])
@pytest.mark.asyncio
async def test_headless_output_is_machine_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    class App:
        session_id = "thread-1"

        async def run(self, prompt: str, **kwargs: object) -> dict[str, object]:
            return {"ok": True, "status": "completed", "response": "回答完毕"}

        async def stream(self, prompt: str, **kwargs: object) -> Any:
            yield {"type": "assistant.delta", "delta": "回答完毕"}
            yield {"type": "run.completed", "ok": True, "response": "回答完毕"}

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "你好", "--output-format", output_format],
        app_factory=lambda _: App(),
    )
    out, err = capsys.readouterr()
    assert code == 0 and err == "" and "\x1b[" not in out
    if output_format == "text":
        assert out == "回答完毕\n"
    elif output_format == "json":
        assert json.loads(out)["response"] == "回答完毕"
        assert len(out.splitlines()) == 1
    else:
        assert [json.loads(line)["type"] for line in out.splitlines()] == [
            "run.started", "assistant.delta", "run.completed"
        ]


@pytest.mark.asyncio
async def test_jsonl_orders_events_and_redacts_tool_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class App:
        session_id = "thread-1"

        async def stream(self, prompt: str, **kwargs: object) -> Any:
            yield {"type": "assistant.delta", "delta": "hello"}
            yield {
                "type": "tool.started",
                "tool_name": "read_file",
                "tool_call_id": "call-1",
                "thread_id": "task-child",
                "task_id": "child",
                "agent_role": "reviewer",
                "args": {"api_key": "very-secret"},
            }
            yield {"type": "run.completed", "ok": True, "response": "hello"}

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "hello", "--output-format", "jsonl"],
        app_factory=lambda _: App(),
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert [event["type"] for event in events] == [
        "run.started", "assistant.delta", "tool.started", "run.completed"
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert all(event["schema_version"] == 1 for event in events)
    assert "very-secret" not in json.dumps(events)
    assert "args" not in events[2]
    assert (events[2]["thread_id"], events[2]["task_id"]) == ("task-child", "child")


@pytest.mark.asyncio
async def test_jsonl_review_decision_precedes_final_event_without_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class App:
        session_id = "thread-1"

        async def run(self, prompt: str, **kwargs: object) -> dict[str, object]:
            return {"ok": True, "status": "completed", "response": "done"}

        def drain_notifications(self) -> list[dict[str, object]]:
            return [
                {
                    "type": "review.decision",
                    "action": "allow",
                    "api_key": "must-not-leak",
                }
            ]

    code = await amain(
        [
            "--workspace", str(tmp_path), "-p", "hello", "--output-format", "jsonl",
            "--no-stream",
        ],
        app_factory=lambda _: App(),
    )
    output = capsys.readouterr().out
    assert code == 0 and "must-not-leak" not in output
    assert [json.loads(line)["type"] for line in output.splitlines()] == [
        "run.started", "review.decision", "run.completed"
    ]


@pytest.mark.asyncio
async def test_memory_event_is_redacted_and_precedes_terminal_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Memory:
        async def flush_headless(self, thread_id: str) -> None:
            assert thread_id == "thread-1"

    class App:
        session_id = "thread-1"
        memory = Memory()

        async def run(self, prompt: str, **kwargs: object) -> dict[str, object]:
            return {
                "ok": True, "status": "completed", "thread_id": "thread-1", "response": "done"
            }

        def drain_notifications(self) -> list[dict[str, object]]:
            return [
                {
                    "type": "memory.updated", "thread_id": "thread-1", "count": 1,
                    "memory_text": "SECRET_MUST_NOT_OUTPUT",
                }
            ]

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "hello", "--output-format", "jsonl",
         "--no-stream"],
        app_factory=lambda _: App(),
    )
    output = capsys.readouterr().out
    assert code == 0 and "SECRET_MUST_NOT_OUTPUT" not in output
    assert [json.loads(line)["type"] for line in output.splitlines()] == [
        "run.started", "memory.updated", "run.completed"
    ]


@pytest.mark.parametrize(
    ("task_status", "expected_code", "terminal"),
    [("idle", 0, "run.completed"), ("paused", 3, "run.paused"), ("failed", 1, "run.failed")],
)
@pytest.mark.asyncio
async def test_jsonl_waits_for_child_before_terminal_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    task_status: str,
    expected_code: int,
    terminal: str,
) -> None:
    class App:
        async def stream(self, prompt: str, **kwargs: object) -> Any:
            yield {"type": "task.started", "task_id": "task-1", "status": "running"}
            yield {"type": "run.completed", "ok": True, "response": "delegated"}

        async def wait_for_tasks(self) -> list[dict[str, object]]:
            return [{"task_id": "task-1", "status": task_status}]

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "delegate", "--output-format", "jsonl"],
        app_factory=lambda _: App(),
    )
    events = [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()]
    assert code == expected_code
    assert events == ["run.started", "task.started", f"task.{task_status}", terminal]


@pytest.mark.asyncio
async def test_headless_pause_uses_exit_code_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class App:
        async def run(self, prompt: str, **kwargs: object) -> dict[str, object]:
            return {"ok": False, "status": "paused", "thread_id": "thread-1"}

    code = await amain(
        ["--workspace", str(tmp_path), "-p", "needs approval", "--output-format", "json"],
        app_factory=lambda _: App(),
    )
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "paused"


@pytest.mark.asyncio
async def test_skill_activates_before_headless_model_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, str]] = []

    class App:
        async def activate_skill(self, name: str) -> None:
            calls.append(("skill", name))

        async def run(self, prompt: str, **kwargs: object) -> dict[str, object]:
            calls.append(("run", prompt))
            return {"ok": True, "status": "completed", "response": "done"}

    code = await amain(
        [
            "--workspace", str(tmp_path), "--skill", "review", "-p", "inspect",
            "--output-format", "json", "--no-stream",
        ],
        app_factory=lambda _: App(),
    )
    assert code == 0 and calls == [("skill", "review"), ("run", "inspect")]
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.asyncio
async def test_unknown_skill_returns_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class App:
        async def activate_skill(self, name: str) -> None:
            raise KeyError(name)

    code = await amain(
        ["--workspace", str(tmp_path), "--skill", "missing", "-p", "inspect",
         "--output-format", "json"],
        app_factory=lambda _: App(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["status"] == "config_error"


def test_tool_preview_is_bounded_and_redacted() -> None:
    public = _public_event(
        {
            "type": "tool.started",
            "tool_name": "write_file",
            "tool_call_id": "call-1",
            "tool_input": {"path": "demo.py", "content": "x" * 500, "api_key": "do-not-show"},
        }
    )
    assert public["tool_input"]["path"] == "demo.py"
    assert public["tool_input"]["api_key"] == "***"
    assert len(public["tool_input"]["content"]) <= 241
    assert "do-not-show" not in str(public)


def test_headless_model_flags_are_explicit() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["--protocol", "openai_responses", "--model-id", "model-id",
         "--context-length", "128k", "--max-output-tokens", "8k", "-p", "hello"]
    )
    assert (args.context_length, args.max_output_tokens) == (128000, 8000)
    with pytest.raises(SystemExit):
        parser.parse_args(["--model", "openai:model-id"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--api-key", "test", "--no-api-key"])


def test_real_cli_subprocess_returns_failure_without_model(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["SAYACODE_HOME"] = str(tmp_path / "state")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "sayacode", "--workspace", str(tmp_path),
         "-p", "hello", "--output-format", "json"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False
