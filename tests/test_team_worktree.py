import subprocess

import pytest

from lib.core.team_worktree import TeamWorktreeManager, WorktreeIsolationError


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "SAYACODE Tests")
    (repo / "README.md").write_text("source\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_prepare_creates_isolated_branch_and_retains_source(tmp_path):
    repo = _clean_repo(tmp_path)
    manager = TeamWorktreeManager(tmp_path / "home" / "worktrees")

    worktree = manager.prepare("w1234abcd", repo)
    isolated = tmp_path / "home" / "worktrees" / "w1234abcd"

    assert worktree.worktree_root == str(isolated.resolve())
    assert worktree.workspace == str(isolated.resolve())
    assert worktree.branch == "sayacode/team-w1234abcd"
    assert _git(isolated, "branch", "--show-current") == worktree.branch

    (isolated / "README.md").write_text("worker\n", encoding="utf-8")
    delivery = manager.inspect(isolated, source_commit=worktree.source_commit)

    assert "README.md" in delivery["status"]
    assert "README.md" in delivery["diff_stat"]
    assert (repo / "README.md").read_text(encoding="utf-8") == "source\n"


def test_prepare_rejects_dirty_source_repo(tmp_path):
    repo = _clean_repo(tmp_path)
    (repo / "local-change.txt").write_text("dirty", encoding="utf-8")
    manager = TeamWorktreeManager(tmp_path / "home" / "worktrees")

    with pytest.raises(WorktreeIsolationError, match="源 Git 工作区干净"):
        manager.prepare("w1234abcd", repo)


def test_inspect_rejects_paths_outside_team_directory(tmp_path):
    manager = TeamWorktreeManager(tmp_path / "home" / "worktrees")
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(WorktreeIsolationError, match="团队目录之外"):
        manager.inspect(outside)


def test_prepare_rejects_invalid_worker_id_before_git(tmp_path):
    manager = TeamWorktreeManager(tmp_path / "home" / "worktrees")

    with pytest.raises(WorktreeIsolationError, match="invalid worker_id"):
        manager.prepare("../escape", tmp_path)
