"""Git 工作树快照及显式交付，只做隔离不做自动合并。

建造者任务先对当前仓库做快照提交，再开分支建隔离工作树。
查看只算差异不合入，应用靠补丁合入，清理前必须先合干净。
所有操作只认管理目录下的直接子目录，越界一律拒绝。"""

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
    """一次隔离工作树的建档，记住根目录工作区的分身分支和快照。

    根是管理目录下的任务目录，工作区是原工作区在里面的对应位置。
    分支按任务编号命名，快照是含脏改动的临时提交。"""
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
    """在指定目录跑一条命令并收输出，失败转成任务错误。

    单条命令最多等九十秒，超时或起不来都算失败。
    要检查时非零退出会截断取尾四千字报错，不要检查只看返回码。"""
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
    """跑一条命令并取文本输出，首尾空白会被去掉。

    失败直接抛任务错误，调用方不用再判返回码。"""
    return _run_git(cwd, *args, env=env).stdout.decode("utf-8", "replace").strip()


class WorktreeManager:
    """创建隔离可写任务工作树，不动调用方索引。

    做什么，管快照建树查看合入和清理，全走命令行。
    参数与返回，构造时绑定管理根目录，会自动建好。
    调用约束，快照用临时索引做，不碰调用方暂存区。
    坑点是目标目录必须在管理根下直接一层，越界直接拒绝。"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_git_workspace(workspace: Path) -> bool:
        """做什么，判断该目录能否基于已提交版本建工作树。

        参数与返回，入参是工作区目录，返回是否可建。
        调用约束，目录须存在且在仓库里，且已有提交。
        坑点是命令失败只返回否，不抛错，调用方要另查原因。"""
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
        """做什么，按任务编号建隔离工作树并返回快照。

        参数与返回，入参是任务编号和原工作区，返回快照档案。
        调用约束，分四步走，校验仓库，定分支，做快照，建树验目录。
        坑点是建树失败会删分支，已存在目标目录会直接报错。"""
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
        """做什么，算工作树相对快照的差异，只读不合入。

        参数与返回，入参是任务档案，返回分支状态统计补丁和忽略文件。
        调用约束，档案须带工作树根和快照号，根须在管理目录下。
        坑点是新增未跟踪文件会先占位再算差异，忽略文件只上报不算入。"""
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
        """做什么，把工作树差异合到原仓库，先校验再应用。

        参数与返回，入参是任务档案，返回是否合入和差异原文。
        调用约束，空差异直接返回未合，已合过的靠哈希去重。
        坑点是合后工作树再改会拒绝合，要先看新差异再下新任务。"""
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
        """做什么，删掉隔离工作树，删前先卡交付是否干净。

        参数与返回，入参是任务档案，无返回。
        调用约束，有忽略文件残留不可删，有未合差异不可删。
        坑点是删前会反向校验补丁可逆，删后分支和目录一起没。"""
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
        """做什么，用当前改动建临时提交，含暂存未暂存和未跟踪文件。

        参数与返回，入参是仓库根和任务编号，返回快照提交号。
        调用约束，分四步走，读头树，合脏差异，加未跟踪，写树提交。
        坑点是全程用临时索引，调用方索引和工作区都不会动。"""
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
        """把新增未跟踪文件做占位登记，以便差异能算全。

        无新增直接返回，有新增就逐个占位，不提交。"""
        names = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
        paths = [item for item in names.decode("utf-8", "surrogateescape").split("\0") if item]
        if paths:
            _run_git(root, "add", "-N", "--", *paths)

    @staticmethod
    def _ignored_untracked(root: Path) -> list[str]:
        """列出被忽略的未跟踪文件，用于清理前卡门。

        有残留要人工搬走再清，工具不会自动删。"""
        names = _run_git(
            root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"
        ).stdout
        return [name for name in names.decode("utf-8", "surrogateescape").split("\0") if name]

    def _assert_managed(self, target: Path) -> None:
        """断言目标是管理目录下的直接子目录，越界直接报错。

        所有会改文件系统的动作前都会先过这一关。"""
        if target.parent != self.base_dir:
            raise TaskError("Refusing to operate outside direct SAYACODE worktrees")
