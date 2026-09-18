"""本地私有状态文件的读写辅助函数。

负责创建私有目录并以原子写入加固文件权限。
核心函数：ensure_private_dir、write_private_json。
调用链：paths／audit→ensure_private_dir→write_private。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def restrict_permissions(path: str | Path, directory: bool = False) -> None:
    """尽力而为的本地状态文件权限加固。"""
    if os.name == "nt":
        _restrict_windows_permissions(path, directory=directory)
        return
    try:
        Path(path).chmod(0o700 if directory else 0o600)
    except Exception:
        pass


def _restrict_windows_permissions(path: str | Path, directory: bool = False) -> None:
    """Windows 上针对私有状态路径的尽力而为 ACL 加固。"""
    target = Path(path)
    if not target.exists():
        return

    user = _current_windows_user()
    if not user:
        return

    grant = f"{user}:(OI)(CI)F" if directory else f"{user}:(F)"
    try:
        subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", grant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except Exception:
        pass


def _current_windows_user() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    username = os.environ.get("USERNAME", "").strip()
    if username:
        return f"{domain}\\{username}" if domain else username
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""


def ensure_private_dir(path: str | Path) -> Path:
    """创建用于本地私有状态的目录。"""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    restrict_permissions(directory, directory=True)
    return directory


def write_private_text(path: str | Path, content: str, encoding: str = "utf-8") -> Path:
    """以原子方式写入私有文本，并应用限制性权限。"""
    target = Path(path)
    ensure_private_dir(target.parent)
    tmp_path = target.with_name(target.name + ".tmp")
    try:
        tmp_path.unlink()
    except OSError:
        pass
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise
    restrict_permissions(tmp_path, directory=False)
    os.replace(str(tmp_path), str(target))
    restrict_permissions(target, directory=False)
    return target


def write_private_json(path: str | Path, data: Any) -> Path:
    """以原子方式写入私有 JSON，并应用限制性权限。"""
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
