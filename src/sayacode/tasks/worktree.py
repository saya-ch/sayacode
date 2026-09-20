"""Git 工作树快照及显式交付。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .records import TaskError, TaskRecord


@dataclass(slots=True)
class WorktreeSnapshot:
    root: Path
    workspace: Path
    branch: str
    snapshot_commit: str


def _run_git(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            input=input_bytes,
            capture_output=True,
            env=env,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TaskError(f"Git command failed: {exc}") from exc
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).decode("utf-8", "replace").strip()
        raise TaskError(detail[-4000:] or f"git {' '.join(args)} failed")
    return completed


def _git_text(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return _run_git(cwd, *args, env=env).stdout.decode("utf-8", "replace").strip()


class WorktreeManager:
    """创建隔离可写任务工作树。不动调用方索引。"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_git_workspace(workspace: Path) -> bool:
        """该目录能否基于已提交版本建工作树。"""
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            return False
        try:
            return (
                _run_git(workspace, "rev-parse", "--show-toplevel", check=False).returncode == 0
                and _run_git(workspace, "rev-parse", "--verify", "HEAD", check=False).returncode
                == 0
            )
        except TaskError:
            return False

    def create(self, task_id: str, workspace: Path) -> WorktreeSnapshot:
        workspace = workspace.resolve()
        if not self.is_git_workspace(workspace):
            raise TaskError("Writable background tasks require a Git workspace")
        repo_root = Path(_git_text(workspace, "rev-parse", "--show-toplevel")).resolve()
        try:
            relative_workspace = workspace.relative_to(repo_root)
        except ValueError as exc:
            raise TaskError("Workspace must be inside the Git repository") from exc
        target_root = (self.base_dir / task_id).resolve()
        try:
            target_root.relative_to(self.base_dir)
        except ValueError as exc:
            raise TaskError("Worktree path escaped the SAYACODE worktree directory") from exc
        if target_root.exists():
            raise TaskError(f"Task worktree already exists: {target_root}")

        branch = f"sayacode/task-{task_id}"
        snapshot_commit = self._snapshot_commit(repo_root, task_id)
        try:
            _run_git(repo_root, "worktree", "add", "-b", branch, str(target_root), snapshot_commit)
        except Exception:
            _run_git(repo_root, "branch", "-D", branch, check=False)
            raise
        task_workspace = (target_root / relative_workspace).resolve()
        if not task_workspace.is_dir():
            _run_git(repo_root, "worktree", "remove", "--force", str(target_root), check=False)
            _run_git(repo_root, "branch", "-D", branch, check=False)
            raise TaskError("Created worktree did not contain the requested workspace")
        return WorktreeSnapshot(target_root, task_workspace, branch, snapshot_commit)

    def inspect(self, record: TaskRecord) -> dict[str, Any]:
        if not record.worktree_root or not record.snapshot_commit:
            raise TaskError("Task has no writable worktree")
        root = Path(record.worktree_root).resolve()
        self._assert_managed(root)
        self._stage_intent_to_add(root)
        patch = _run_git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            "--full-index",
            record.snapshot_commit,
        ).stdout.decode("utf-8", "surrogateescape")
        return {
            "task_id": record.task_id,
            "branch": _git_text(root, "branch", "--show-current"),
            "status": _git_text(root, "status", "--short", "--branch"),
            "stat": _git_text(
                root,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--stat",
                record.snapshot_commit,
            ),
            "patch": patch,
            "patch_sha256": hashlib.sha256(patch.encode("utf-8", "surrogateescape")).hexdigest(),
            "ignored_paths": self._ignored_untracked(root),
        }

    def apply_delivery(self, record: TaskRecord) -> dict[str, Any]:
        if not record.worktree_root or not record.snapshot_commit:
            raise TaskError("Task has no delivery to apply")
        source_workspace = Path(record.workspace).resolve()
        root = Path(record.worktree_root).resolve()
        self._assert_managed(root)
        repo_root = Path(_git_text(source_workspace, "rev-parse", "--show-toplevel")).resolve()
        delivery = self.inspect(record)
        patch = str(delivery["patch"])
        if not patch.strip():
            return {"applied": False, "reason": "Task has no changes", "delivery": delivery}
        if record.delivery_state == "applied":
            if delivery["patch_sha256"] == record.applied_patch_sha256:
                return {
                    "applied": False,
                    "reason": "Delivery was already applied",
                    "delivery": delivery,
                }
            raise TaskError(
                "Worktree changed after delivery; inspect the new diff before a new task"
            )
        raw = patch.encode("utf-8", "surrogateescape")
        _run_git(repo_root, "apply", "--check", "--binary", "-", input_bytes=raw)
        _run_git(repo_root, "apply", "--binary", "-", input_bytes=raw)
        return {"applied": True, "delivery": delivery}

    def remove(self, record: TaskRecord) -> None:
        if not record.worktree_root:
            return
        root = Path(record.worktree_root).resolve()
        self._assert_managed(root)
        delivery = self.inspect(record)
        patch = str(delivery["patch"])
        if delivery["ignored_paths"]:
            raise TaskError(
                "Ignored files remain in the worktree; move or remove them explicitly before cleanup"
            )
        if patch:
            if record.delivery_state != "applied" or not record.applied_patch_sha256:
                raise TaskError("Unapplied task changes remain; inspect and apply delivery first")
            if delivery["patch_sha256"] != record.applied_patch_sha256:
                raise TaskError("Task changed after delivery was applied; inspect the new diff")
        source_workspace = Path(record.workspace).resolve()
        repo_root = Path(_git_text(source_workspace, "rev-parse", "--show-toplevel")).resolve()
        if patch:
            _run_git(
                repo_root,
                "apply",
                "--reverse",
                "--check",
                "--binary",
                "-",
                input_bytes=patch.encode("utf-8", "surrogateescape"),
            )
        _run_git(repo_root, "worktree", "remove", "--force", str(root))

    def _snapshot_commit(self, repo_root: Path, task_id: str) -> str:
        """用当前改动建临时提交。含暂存未暂存和未跟踪文件。"""
        with tempfile.TemporaryDirectory(prefix="sayacode-index-") as temp_dir:
            index = str(Path(temp_dir) / "index")
            env = {
                **os.environ,
                "GIT_INDEX_FILE": index,
                "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "SAYACODE"),
                "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "sayacode@local"),
                "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "SAYACODE"),
                "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "sayacode@local"),
            }
            _run_git(repo_root, "read-tree", "HEAD", env=env)
            diff = _run_git(
                repo_root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"
            ).stdout
            if diff:
                _run_git(repo_root, "apply", "--cached", "--binary", "-", env=env, input_bytes=diff)
            untracked = (
                _run_git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
                .stdout.decode("utf-8", "surrogateescape")
                .split("\0")
            )
            paths = [path for path in untracked if path]
            if paths:
                _run_git(repo_root, "add", "--force", "--", *paths, env=env)
            tree = _git_text(repo_root, "write-tree", env=env)
            parent = _git_text(repo_root, "rev-parse", "HEAD")
            return _git_text(
                repo_root,
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                f"SAYACODE task snapshot {task_id}",
                env=env,
            )

    def _stage_intent_to_add(self, root: Path) -> None:
        names = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
        paths = [item for item in names.decode("utf-8", "surrogateescape").split("\0") if item]
        if paths:
            _run_git(root, "add", "-N", "--", *paths)

    @staticmethod
    def _ignored_untracked(root: Path) -> list[str]:
        names = _run_git(
            root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"
        ).stdout
        return [name for name in names.decode("utf-8", "surrogateescape").split("\0") if name]

    def _assert_managed(self, target: Path) -> None:
        if target.parent != self.base_dir:
            raise TaskError("Refusing to operate outside direct SAYACODE worktrees")
