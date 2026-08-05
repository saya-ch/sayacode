import subprocess

from lib.core.team_manager import TeamManager
from lib.core.worker_manager import WorkerState, WorkerStatus


def test_team_manager_queues_task_and_selects_read_only_mode(tmp_path, monkeypatch):
    manager = TeamManager(tmp_path / "home")
    monkeypatch.setattr(manager.workers, "new_worker_id", lambda: "w1234abcd")
    captured = {}

    def fake_spawn(config, *, worker_id=None):
        captured.update(config)
        manager.workers._workers[worker_id] = WorkerState(
            worker_id=worker_id,
            status=WorkerStatus.RUNNING,
            config=config,
        )
        return worker_id

    monkeypatch.setattr(manager.workers, "spawn", fake_spawn)

    worker_id = manager.spawn("reviewer", "inspect auth", workspace=str(tmp_path))

    assert worker_id == "w1234abcd"
    assert captured["mode"] == "review"
    task = manager.get_mailbox(worker_id).read_unread()[0]
    assert task.sender == "leader"
    assert task.content["task"] == "inspect auth"


def test_team_manager_reads_worker_result_from_leader_mailbox(tmp_path):
    manager = TeamManager(tmp_path / "home")
    manager.workers._workers["w1234abcd"] = WorkerState(
        worker_id="w1234abcd",
        status=WorkerStatus.COMPLETED,
    )
    manager.get_mailbox("leader").write(
        {
            "type": "result",
            "ok": True,
            "worker_id": "w1234abcd",
            "response": "done",
        },
        sender="w1234abcd",
    )

    result = manager.get_result("w1234abcd")

    assert result is not None
    assert result["response"] == "done"
    assert manager.get_mailbox("leader").unread_count == 0


def test_team_manager_routes_builder_to_isolated_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess_args = {"check": True, "capture_output": True, "text": True}

    subprocess.run(["git", "-C", str(repo), "init"], **subprocess_args)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], **subprocess_args)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "SAYACODE Tests"], **subprocess_args)
    (repo / "README.md").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], **subprocess_args)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], **subprocess_args)

    manager = TeamManager(tmp_path / "home")
    monkeypatch.setattr(manager.workers, "new_worker_id", lambda: "w1234abcd")
    captured = {}

    def fake_spawn(config, *, worker_id=None):
        captured.update(config)
        manager.workers._workers[worker_id] = WorkerState(
            worker_id=worker_id,
            status=WorkerStatus.RUNNING,
            config=config,
            worktree=config["worktree"],
        )
        return worker_id

    monkeypatch.setattr(manager.workers, "spawn", fake_spawn)

    manager.spawn("builder", "implement feature", workspace=str(repo))

    assert captured["workspace"] != str(repo.resolve())
    assert captured["worktree"] == captured["workspace"]
    assert captured["branch"] == "sayacode/team-w1234abcd"
    assert (repo / "README.md").read_text(encoding="utf-8") == "source\n"


def test_team_manager_allows_explicit_shared_builder_without_git(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = TeamManager(tmp_path / "home")
    monkeypatch.setattr(manager.workers, "new_worker_id", lambda: "w1234abcd")
    captured = {}

    def fake_spawn(config, *, worker_id=None):
        captured.update(config)
        manager.workers._workers[worker_id] = WorkerState(
            worker_id=worker_id,
            status=WorkerStatus.RUNNING,
            config=config,
        )
        return worker_id

    monkeypatch.setattr(manager.workers, "spawn", fake_spawn)

    manager.spawn("shared-builder", "implement feature", workspace=str(workspace))

    assert captured["workspace"] == str(workspace.resolve())
    assert captured["worktree"] == ""
