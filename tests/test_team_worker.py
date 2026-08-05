import json
from pathlib import Path
from types import SimpleNamespace

from lib.core.agent_mailbox import AgentMailbox
from lib.core.team_worker import execute_mailbox_task


def test_team_worker_consumes_mailbox_and_publishes_result(tmp_path, monkeypatch):
    base_dir = tmp_path / "home"
    workspace = tmp_path / "workspace"
    base_dir.mkdir()
    workspace.mkdir()
    (base_dir / "api_configs.json").write_text('{"secret":"not-printed"}', encoding="utf-8")
    (base_dir / "user_config.json").write_text("{}", encoding="utf-8")
    inbox = AgentMailbox(base_dir, "w1234abcd")
    inbox.write(
        {"type": "task", "task": "inspect runtime", "agent_type": "reviewer"},
        sender="leader",
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs["input"]
        isolated_home = Path(kwargs["env"]["SAYACODE_HOME"])
        assert isolated_home != base_dir
        assert (isolated_home / "api_configs.json").is_file()
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "response": "review complete",
                "model_type": "openai",
                "model_name": "unit-model",
                "session_id": "child-session",
            }),
            stderr="",
        )

    monkeypatch.setattr("lib.core.team_worker.subprocess.run", fake_run)

    result = execute_mailbox_task(
        base_dir=base_dir,
        worker_id="w1234abcd",
        workspace=workspace,
        mode="review",
    )

    assert result["ok"] is True
    assert result["response"] == "review complete"
    assert "inspect runtime" in captured["input"]
    assert "--output-format" in captured["cmd"]
    assert inbox.read_all(include_read=True)[0].is_read is True
    leader_results = AgentMailbox(base_dir, "leader").read_unread()
    assert leader_results[0].content["worker_id"] == "w1234abcd"
    assert leader_results[0].content["response"] == "review complete"
