import json
from pathlib import Path

import pytest

from lib.core.worker_manager import WorkerManager, WorkerStatus


@pytest.fixture(autouse=True)
def _plain_private_writer(monkeypatch):
    def write_json(path, payload):
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr("lib.core.worker_manager.write_private_json", write_json)


class FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


def _spawn_config(tmp_path):
    return {
        "agent_type": "reviewer",
        "workspace": str(tmp_path),
        "mode": "review",
        "sayacode_home": str(tmp_path / "home"),
    }


def test_worker_manager_launches_mailbox_worker_without_pipe_deadlock(tmp_path, monkeypatch):
    captured = {}
    process = FakeProcess()

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr("lib.core.worker_manager.subprocess.Popen", fake_popen)
    manager = WorkerManager(tmp_path / "workers")

    worker_id = manager.spawn(_spawn_config(tmp_path), worker_id="w1234abcd")
    state = manager.get_state(worker_id)

    assert state is not None
    assert state.status == WorkerStatus.RUNNING
    assert state.pid == 4242
    assert "lib.core.team_worker" in captured["cmd"]
    assert captured["kwargs"]["stdout"] is not None
    assert captured["kwargs"]["stderr"] is not None
    assert captured["kwargs"]["stdout"] != -1
    assert captured["kwargs"]["stderr"] != -1
    assert (tmp_path / "workers" / "w1234abcd.state.json").is_file()


def test_worker_manager_reads_structured_result_and_persists_completion(tmp_path, monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(
        "lib.core.worker_manager.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    manager = WorkerManager(tmp_path / "workers")
    worker_id = manager.spawn(_spawn_config(tmp_path), worker_id="w1234abcd")
    state = manager.get_state(worker_id)
    assert state is not None

    payload = {"ok": True, "worker_id": worker_id, "response": "done"}
    with open(state.stdout_path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload))
    process.returncode = 0

    result = manager.get_result(worker_id)

    assert result == payload
    assert manager.get_state(worker_id).status == WorkerStatus.COMPLETED
    persisted = json.loads((tmp_path / "workers" / "w1234abcd.state.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "completed"


def test_worker_manager_enforces_concurrency_limit(tmp_path, monkeypatch):
    next_pid = iter((1001, 1002))
    monkeypatch.setattr(
        "lib.core.worker_manager.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(next(next_pid)),
    )
    manager = WorkerManager(tmp_path / "workers", max_workers=1)
    manager.spawn(_spawn_config(tmp_path), worker_id="w1234abcd")

    with pytest.raises(RuntimeError, match="最多同时运行 1 个"):
        manager.spawn(_spawn_config(tmp_path), worker_id="w5678abcd")
