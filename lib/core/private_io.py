"""Private local-state file helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def restrict_permissions(path: str | Path, directory: bool = False) -> None:
    """Best-effort local-state permission hardening (Unix only; no-op on Windows)."""
    import os as _os
    if _os.name == "nt":
        return
    try:
        Path(path).chmod(0o700 if directory else 0o600)
    except Exception:
        pass


def ensure_private_dir(path: str | Path) -> Path:
    """Create a directory intended for local private state."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    restrict_permissions(directory, directory=True)
    return directory


def write_private_text(path: str | Path, content: str, encoding: str = "utf-8") -> Path:
    """Atomically write private text with restrictive permissions."""
    target = Path(path)
    ensure_private_dir(target.parent)
    tmp_path = target.with_name(target.name + ".tmp")

    tmp_path.write_text(content, encoding=encoding)
    restrict_permissions(tmp_path, directory=False)
    tmp_path.replace(target)
    restrict_permissions(target, directory=False)
    return target


def write_private_json(path: str | Path, data: Any) -> Path:
    """Atomically write private JSON with restrictive permissions."""
    return write_private_text(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "ensure_private_dir",
    "restrict_permissions",
    "write_private_json",
    "write_private_text",
]
