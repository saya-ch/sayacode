"""Central SAYACODE path and local-state store services."""

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
    """Resolved locations for SAYACODE user and workspace state."""

    home: Path

    @classmethod
    def resolve(cls, home: Optional[str | Path] = None, *, create: bool = False) -> "SayacodePaths":
        """Resolve the user state root, honoring SAYACODE_HOME."""
        raw_home = home or os.environ.get("SAYACODE_HOME")
        path = Path(raw_home).expanduser() if raw_home else Path.home() / ".sayacode"
        resolved = path.resolve()
        if create:
            ensure_private_dir(resolved)
        return cls(home=resolved)

    @property
    def user_config(self) -> Path:
        return self.home / "user_config.json"

    @property
    def api_configs(self) -> Path:
        return self.home / "api_configs.json"

    @property
    def sessions_dir(self) -> Path:
        return self.home / "sessions"

    @property
    def user_permissions(self) -> Path:
        return self.home / "permissions.json"

    @property
    def user_hooks(self) -> Path:
        return self.home / "hooks.json"

    @property
    def hook_trusted_projects(self) -> Path:
        return self.home / "trusted_projects.json"

    @property
    def mcp_trusted_projects(self) -> Path:
        return self.home / "mcp_trusted_projects.json"

    @property
    def user_memory(self) -> Path:
        return self.home / "memory.md"

    @property
    def audit_log(self) -> Path:
        return self.home / "audit.jsonl"

    def workspace_state_dir(self, workspace: str | Path) -> Path:
        return self.sessions_dir / _workspace_slug(workspace)

    def workspace_state_paths(self, workspace: str | Path) -> Dict[str, Path]:
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
        paths = self.workspace_state_paths(workspace)
        session_dir = paths["sessions_dir"] / _session_dir_name(session_id)
        return {
            "dir": session_dir,
            "session": session_dir / "session.json",
            "memory": session_dir / "memory.json",
            "context": session_dir / "context.json",
        }

    def project_permissions(self, workspace: str | Path) -> Path:
        return Path(workspace).expanduser().resolve() / ".sayacode" / "permissions.json"

    def project_hooks(self, workspace: str | Path) -> Path:
        return Path(workspace).expanduser().resolve() / ".sayacode" / "hooks.json"


class ConfigStore:
    """Small JSON store for user-scoped configuration files."""

    def __init__(self, paths: Optional[SayacodePaths] = None) -> None:
        self.paths = paths or SayacodePaths.resolve(create=True)

    def read_json(self, path: str | Path, default: Any = None) -> Any:
        target = Path(path)
        if not target.exists():
            return default
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return default

    def write_json(self, path: str | Path, data: Any) -> Path:
        return write_private_json(path, data)


class StateStore:
    """Workspace-scoped state path helper."""

    def __init__(self, paths: Optional[SayacodePaths] = None) -> None:
        self.paths = paths or SayacodePaths.resolve(create=True)

    def workspace_state_dir(self, workspace: str | Path) -> Path:
        return self.paths.workspace_state_dir(workspace)

    def workspace_state_paths(self, workspace: str | Path) -> Dict[str, Path]:
        return self.paths.workspace_state_paths(workspace)

    def workspace_session_paths(self, workspace: str | Path, session_id: str) -> Dict[str, Path]:
        return self.paths.workspace_session_paths(workspace, session_id)

    def write_text(self, path: str | Path, content: str) -> Path:
        return write_private_text(path, content, encoding="utf-8")

    def write_json(self, path: str | Path, data: Any) -> Path:
        return write_private_json(path, data)


__all__ = [
    "ConfigStore",
    "SayacodePaths",
    "StateStore",
]
