"""本地安装拥有的文件位置。

目录存放应用元数据和图数据库。会话消息只在检查点库中。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


@dataclass(frozen=True, slots=True)
class AppPaths:
    """解析后的一套本地安装路径。"""

    home: Path

    @classmethod
    def resolve(cls, home: str | Path | None = None, *, create: bool = True) -> "AppPaths":
        configured = home or os.environ.get("SAYACODE_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".sayacode"
        root = root.resolve()
        if create:
            _private_dir(root)
        return cls(root)

    @property
    def config(self) -> Path:
        return self.home / "config.json"

    @property
    def checkpoints(self) -> Path:
        return self.home / "checkpoints.sqlite3"

    @property
    def store(self) -> Path:
        return self.home / "store.sqlite3"

    @property
    def audit(self) -> Path:
        return self.home / "audit.jsonl"

    @property
    def outputs(self) -> Path:
        return _private_dir(self.home / "outputs")

    @property
    def worktrees(self) -> Path:
        return _private_dir(self.home / "worktrees")

    @property
    def hooks(self) -> Path:
        return self.home / "hooks.json"

    @property
    def user_commands(self) -> Path:
        return _private_dir(self.home / "commands")

    @property
    def memory(self) -> Path:
        return self.home / "memory.md"

    def project_root(self, workspace: Path) -> Path:
        return workspace / ".sayacode"

    def project_hooks(self, workspace: Path) -> Path:
        return self.project_root(workspace) / "hooks.json"

    def project_commands(self, workspace: Path) -> Path:
        return self.project_root(workspace) / "commands"


__all__ = ["AppPaths"]
