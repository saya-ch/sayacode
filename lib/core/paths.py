"""SAYACODE 路径与本地状态存储的集中式服务。

负责解析用户级与工作区级状态文件路径并提供读写。
核心类：SayacodePaths、ConfigStore、StateStore。
调用链：各模块→SayacodePaths.resolve→private_io。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import hashlib
import json
import os
import re

from .private_io import ensure_private_dir, write_private_json, write_private_text


def _workspace_slug(workspace: str | Path) -> str:
    resolved = Path(workspace).expanduser().resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", resolved.name or "workspace").strip("-") or "workspace"
    return f"{slug}-{digest}"


def _session_dir_name(session_id: str) -> str:
    raw = str(session_id or "").strip()
    if not raw:
        raise ValueError("session_id cannot be empty")
    if raw in {".", ".."} or "/" in raw or "\\" in raw:
        raise ValueError("session_id cannot contain path separators")
    if Path(raw).is_absolute() or re.match(r"^[a-zA-Z]:", raw):
        raise ValueError("session_id cannot be an absolute path")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", raw):
        raise ValueError("session_id contains unsupported characters")
    return raw


@dataclass(frozen=True)
class SayacodePaths:
    """SAYACODE 用户级与工作区状态文件的解析结果路径。"""

    home: Path

    @classmethod
    def resolve(cls, home: Optional[str | Path] = None, *, create: bool = False) -> "SayacodePaths":
        """解析用户状态根目录，并遵循 SAYACODE_HOME 环境变量。"""
        raw_home = home or os.environ.get("SAYACODE_HOME")
        path = Path(raw_home).expanduser() if raw_home else Path.home() / ".sayacode"
        resolved = path.resolve()
        if create:
            ensure_private_dir(resolved)
        return cls(home=resolved)

    @property
    def user_config(self) -> Path:
        """返回用户配置文件路径。"""
        return self.home / "user_config.json"

    @property
    def api_configs(self) -> Path:
        """返回模型配置存储路径。"""
        return self.home / "api_configs.json"

    @property
    def sessions_dir(self) -> Path:
        """返回会话根目录路径。"""
        return self.home / "sessions"

    @property
    def user_permissions(self) -> Path:
        """返回用户级权限文件路径。"""
        return self.home / "permissions.json"

    @property
    def user_hooks(self) -> Path:
        """返回用户级 hook 文件路径。"""
        return self.home / "hooks.json"

    @property
    def hook_trusted_projects(self) -> Path:
        """返回 hook 信任名单路径。"""
        return self.home / "trusted_projects.json"

    @property
    def mcp_trusted_projects(self) -> Path:
        """返回 MCP 信任名单路径。"""
        return self.home / "mcp_trusted_projects.json"

    @property
    def user_memory(self) -> Path:
        """返回用户记忆文件路径。"""
        return self.home / "memory.md"

    @property
    def audit_log(self) -> Path:
        """返回审计日志文件路径。"""
        return self.home / "audit.jsonl"

    def workspace_state_dir(self, workspace: str | Path) -> Path:
        """返回指定工作区的状态目录。"""
        return self.sessions_dir / _workspace_slug(workspace)

    def workspace_state_paths(self, workspace: str | Path) -> Dict[str, Path]:
        """返回指定工作区的状态文件映射。"""
        state_dir = self.workspace_state_dir(workspace)
        return {
            "dir": state_dir,
            "index": state_dir / "index.json",
            "sessions_dir": state_dir / "sessions",
            "session": state_dir / "session.json",
            "memory": state_dir / "memory.json",
            "context": state_dir / "context.json",
        }

    def workspace_session_paths(self, workspace: str | Path, session_id: str) -> Dict[str, Path]:
        """返回指定会话的文件路径映射。"""
        paths = self.workspace_state_paths(workspace)
        session_dir = paths["sessions_dir"] / _session_dir_name(session_id)
        return {
            "dir": session_dir,
            "session": session_dir / "session.json",
            "memory": session_dir / "memory.json",
            "context": session_dir / "context.json",
        }

    def project_permissions(self, workspace: str | Path) -> Path:
        """返回项目级权限文件路径。"""
        return Path(workspace).expanduser().resolve() / ".sayacode" / "permissions.json"

    def project_hooks(self, workspace: str | Path) -> Path:
        """返回项目级 hook 文件路径。"""
        return Path(workspace).expanduser().resolve() / ".sayacode" / "hooks.json"


class ConfigStore:
    """用于用户级配置文件的轻量 JSON 存储。"""

    def __init__(self, paths: Optional[SayacodePaths] = None) -> None:
        self.paths = paths or SayacodePaths.resolve(create=True)

    def read_json(self, path: str | Path, default: Any = None) -> Any:
        """读取 JSON 文件，缺失则返回默认值。"""
        target = Path(path)
        if not target.exists():
            return default
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return default

    def write_json(self, path: str | Path, data: Any) -> Path:
        """写入用户级 JSON 配置。"""
        return write_private_json(path, data)


class StateStore:
    """工作区级状态路径辅助工具。"""

    def __init__(self, paths: Optional[SayacodePaths] = None) -> None:
        self.paths = paths or SayacodePaths.resolve(create=True)

    def workspace_state_dir(self, workspace: str | Path) -> Path:
        """返回工作区状态目录。"""
        return self.paths.workspace_state_dir(workspace)

    def workspace_state_paths(self, workspace: str | Path) -> Dict[str, Path]:
        """返回工作区状态文件映射。"""
        return self.paths.workspace_state_paths(workspace)

    def workspace_session_paths(self, workspace: str | Path, session_id: str) -> Dict[str, Path]:
        """返回工作区会话文件映射。"""
        return self.paths.workspace_session_paths(workspace, session_id)

    def write_text(self, path: str | Path, content: str) -> Path:
        """以私有权限写入文本状态。"""
        return write_private_text(path, content, encoding="utf-8")

    def write_json(self, path: str | Path, data: Any) -> Path:
        """以私有权限写入 JSON 状态。"""
        return write_private_json(path, data)


__all__ = [
    "ConfigStore",
    "SayacodePaths",
    "StateStore",
]
