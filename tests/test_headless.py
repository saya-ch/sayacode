import io
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from lib.cli.headless import resolve_headless_prompt, run_headless
from lib.cli.parser import build_cli_parser


def _args(tmp_path, **overrides):
    values = {
        "prompt": "return ok",
        "output_format": "json",
        "workspace": str(tmp_path),
        "model_type": None,
        "model_name": None,
        "base_url": None,
        "api_key": None,
        "context_window": None,
        "session": None,
        "new_session": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parser_accepts_headless_options():
    args = build_cli_parser().parse_args(["-p", "hello", "--output-format", "json"])

    assert args.prompt == "hello"
    assert args.output_format == "json"

    jsonl_args = build_cli_parser().parse_args(["-p", "hello", "--output-format", "jsonl"])
    assert jsonl_args.output_format == "jsonl"


def test_resolve_headless_prompt_reads_stdin():
    assert resolve_headless_prompt("-", stdin=io.StringIO("from pipe\n")) == "from pipe"


def test_headless_json_output_is_clean(tmp_path, monkeypatch, capsys):
    closed = []

    class FakeAgent:
        def run(self, prompt):
            print("internal noise")
            return f"answer:{prompt}"

        def close(self):
            closed.append(True)
            raise RuntimeError("cleanup failed")

    fake_state = SimpleNamespace(
        session=SimpleNamespace(session_id="session-1"),
    )
    fake_result = SimpleNamespace(agent=FakeAgent(), state=fake_state)

    monkeypatch.setattr(
        "lib.cli.headless.resolve_launch_model_config",
        lambda **kwargs: ("openai", "unit-model", {"context_window": 4096}, "profile"),
    )
    monkeypatch.setattr(
        "lib.cli.headless.StartupService",
        lambda **kwargs: SimpleNamespace(bootstrap=lambda options: fake_result),
    )
    monkeypatch.setattr("lib.cli.headless.persist_local_state", lambda *args: None)

    code = run_headless(
        _args(tmp_path),
        SimpleNamespace(),
        prompt_style="concise",
        agent_mode="review",
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert captured.err == ""
    assert payload == {
        "ok": True,
        "response": "answer:return ok",
        "model_type": "openai",
        "model_name": "unit-model",
        "session_id": "session-1",
    }
    assert closed == [True]


def test_headless_json_error_is_structured(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "lib.cli.headless.resolve_launch_model_config",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("profile missing")),
    )

    code = run_headless(
        _args(tmp_path),
        SimpleNamespace(),
        prompt_style="concise",
        agent_mode="review",
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["error_type"] == "RuntimeError"
    assert payload["error"] == "profile missing"


def test_headless_jsonl_streams_public_model_and_tool_events(tmp_path, monkeypatch, capsys):
    closed = []

    class FakeAgent:
        def stream_run(self, prompt, *, event_callback, emit_tool_status):
            assert emit_tool_status is False
            event_callback({
                "agent": {
                    "messages": [AIMessage(
                        content="",
                        additional_kwargs={"reasoning_content": "private chain of thought"},
                        tool_calls=[{
                            "name": "read_file",
                            "args": {"path": "README.md", "api_key": "must-not-leak"},
                            "id": "call-1",
                            "type": "tool_call",
                        }],
                    )],
                },
            })
            # Replayed snapshots must not duplicate a provider-identified event.
            event_callback({
                "agent": {
                    "messages": [AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "read_file",
                            "args": {"path": "README.md", "api_key": "must-not-leak"},
                            "id": "call-1",
                            "type": "tool_call",
                        }],
                    )],
                },
            })
            event_callback({
                "tools": {
                    "messages": [ToolMessage(
                        content='{"summary":"ok","token":"must-not-leak"}',
                        name="read_file",
                        tool_call_id="call-1",
                    )],
                },
            })
            yield "answer:"
            yield prompt

        def close(self):
            closed.append(True)

    fake_state = SimpleNamespace(session=SimpleNamespace(session_id="session-jsonl"))
    fake_result = SimpleNamespace(agent=FakeAgent(), state=fake_state)
    monkeypatch.setattr(
        "lib.cli.headless.resolve_launch_model_config",
        lambda **kwargs: ("openai", "unit-model", {"context_window": 4096}, "profile"),
    )
    monkeypatch.setattr(
        "lib.cli.headless.StartupService",
        lambda **kwargs: SimpleNamespace(bootstrap=lambda options: fake_result),
    )
    monkeypatch.setattr("lib.cli.headless.persist_local_state", lambda *args: None)

    code = run_headless(
        _args(tmp_path, output_format="jsonl"),
        SimpleNamespace(),
        prompt_style="concise",
        agent_mode="review",
    )
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines()]

    assert code == 0
    assert captured.err == ""
    assert [line["type"] for line in lines] == [
        "run.started",
        "tool.started",
        "tool.completed",
        "assistant.delta",
        "assistant.delta",
        "run.completed",
    ]
    assert [line["sequence"] for line in lines] == list(range(1, len(lines) + 1))
    assert len({line["run_id"] for line in lines}) == 1
    assert all(line["schema_version"] == 1 for line in lines)
    assert lines[1]["arguments"] == {"path": "README.md", "api_key": "***"}
    assert lines[2]["result"] == {"summary": "ok", "token": "***"}
    assert lines[3]["delta"] == "answer:"
    assert lines[-1]["response"] == "answer:return ok"
    assert lines[-1]["session_id"] == "session-jsonl"
    assert "private chain of thought" not in captured.out
    assert "must-not-leak" not in captured.out
    assert closed == [True]


def test_headless_jsonl_error_is_one_redacted_event(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "lib.cli.headless.resolve_launch_model_config",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("api_key=super-secret-value")),
    )

    code = run_headless(
        _args(tmp_path, output_format="jsonl"),
        SimpleNamespace(),
        prompt_style="concise",
        agent_mode="review",
    )
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert code == 1
    assert len(lines) == 1
    assert lines[0]["type"] == "run.failed"
    assert lines[0]["error_type"] == "RuntimeError"
    assert lines[0]["error"] == "api_key=***"
