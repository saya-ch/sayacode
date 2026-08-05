"""Git worktree isolation for write-capable team workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import subprocess
from typing import Any

from .private_io import ensure_private_dir


_WORKER_ID_RE = re.compile(r"^w[0-9a-f]{8}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


class WorktreeIsolationError(RuntimeError):
    """Raised when a safe isolated worktree cannot be prepared."""


@dataclass(frozen=True)
class TeamWorktree:
    worker_id: str
    source_workspace: str
    repo_root: str
    worktree_root: str
    workspace: str
    branch: str
    source_commit: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TeamWorktreeManager:
    """Create retained per-worker branches under a private team directory."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).expanduser().resolve()
        ensure_private_dir(self.base_dir)

    def prepare(self, worker_id: str, workspace: str | Path) -> TeamWorktree:
        if not _WORKER_ID_RE.fullmatch(worker_id):
            raise WorktreeIsolationError("invalid worker_id")
        source_workspace = Path(workspace).expanduser().resolve()
        if not source_workspace.is_dir():
            raise WorktreeIsolationError(f"工作区不是目录: {source_workspace}")

        repo_root_text = self._git(source_workspace, "rev-parse", "--show-toplevel")
        repo_root = Path(repo_root_text).resolve()
        try:
            relative_workspace = source_workspace.relative_to(repo_root)
        except ValueError as exc:
            raise WorktreeIsolationError("工作区不在 Git 仓库根目录内") from exc

        source_commit = self._git(repo_root, "rev-parse", "HEAD")
        dirty = self._git(repo_root, "status", "--porcelain", "--untracked-files=normal")
        if dirty:
            raise WorktreeIsolationError(
                "写入型子 Agent 要求源 Git 工作区干净；请先提交/移走本地改动，"
                "或显式使用 shared-builder 承担共享工作区风险"
            )

        worktree_root = (self.base_dir / worker_id).resolve()
        try:
            worktree_root.relative_to(self.base_dir)
        except ValueError as exc:
            raise WorktreeIsolationError("worktree path escapes team directory") from exc
        if worktree_root.exists():
            raise WorktreeIsolationError(f"Worker worktree 已存在: {worktree_root}")

        branch = f"sayacode/team-{worker_id}"
        self._git(repo_root, "worktree", "add", "-b", branch, str(worktree_root), source_commit)
        isolated_workspace = (worktree_root / relative_workspace).resolve()
        if not isolated_workspace.is_dir():
            raise WorktreeIsolationError(f"隔离工作区创建失败: {isolated_workspace}")

        return TeamWorktree(
            worker_id=worker_id,
            source_workspace=str(source_workspace),
            repo_root=str(repo_root),
            worktree_root=str(worktree_root),
            workspace=str(isolated_workspace),
            branch=branch,
            source_commit=source_commit,
        )

    def inspect(self, worktree: str | Path, source_commit: str = "") -> dict[str, Any]:
        worktree_root = Path(worktree).expanduser().resolve()
        try:
            worktree_root.relative_to(self.base_dir)
        except ValueError as exc:
            raise WorktreeIsolationError("拒绝检查团队目录之外的 worktree") from exc
        if not worktree_root.is_dir():
            raise WorktreeIsolationError(f"Worker worktree 不存在: {worktree_root}")

        branch = self._git(worktree_root, "branch", "--show-current")
        status = self._git(worktree_root, "status", "--short", "--branch")
        diff_stat = self._git(worktree_root, "diff", "--stat")
        commits = ""
        if source_commit:
            if not _COMMIT_RE.fullmatch(source_commit):
                raise WorktreeIsolationError("invalid source commit")
            commits = self._git(
                worktree_root,
                "log",
                "--oneline",
                f"{source_commit}..HEAD",
            )
        return {
            "worktree": str(worktree_root),
            "branch": branch,
            "status": status,
            "diff_stat": diff_stat,
            "commits": commits,
        }

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeIsolationError(f"Git 命令执行失败: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise WorktreeIsolationError(detail or f"git {' '.join(args)} failed")
        return completed.stdout.strip()


__all__ = ["TeamWorktree", "TeamWorktreeManager", "WorktreeIsolationError"]
