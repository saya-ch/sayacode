"""本地安装拥有的文件位置。

目录存放应用元数据和图数据库。会话消息只在检查点库中。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _private_dir(path: Path) -> Path:
    # 建出私有目录，非视窗系统顺手收紧权限，收不紧也不报错，调用方直接拿返回的路径用。
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


@dataclass(frozen=True, slots=True)
class AppPaths:
    """解析后的一套本地安装路径。根下放配置和库，敏感输出目录自动建好并收紧权限。"""

    home: Path

    @classmethod
    def resolve(cls, home: str | Path | None = None, *, create: bool = True) -> "AppPaths":
        """解析安装根目录。传入指定目录或空，返回路径集合。优先级是传入值先于环境变量再兜底家目录，建目录失败会直接抛错。"""
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
        """返回工作区内的项目配置目录。传入工作区，返回其下配置目录。不建目录，只是拼路径。"""
        return workspace / ".sayacode"

    def project_hooks(self, workspace: Path) -> Path:
        """返回工作区内的钩子配置文件路径。传入工作区，返回文件路径。不读文件不存在也不报错。"""
        return self.project_root(workspace) / "hooks.json"

    def project_commands(self, workspace: Path) -> Path:
        """返回工作区内的自定义命令目录路径。传入工作区，返回目录路径。只拼路径，不建目录。"""
        return self.project_root(workspace) / "commands"


__all__ = ["AppPaths"]
