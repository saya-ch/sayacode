import io
import json
from types import SimpleNamespace

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
