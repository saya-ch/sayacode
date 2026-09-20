"""直接文件工具与完整输出定位。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool

from ..trust import context_value, workspace_path

_IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".sayacode_outputs"}
_EDIT_LOCK = threading.RLock()


def _root(runtime: ToolRuntime) -> Path:
    return workspace_path(runtime.context)


def _path(runtime: ToolRuntime, path: str) -> Path:
    return workspace_path(runtime.context, path)


def _output_dir(runtime: ToolRuntime) -> Path:
    value = context_value(runtime.context, "output_dir")
    result = Path(value) if value else _root(runtime) / ".sayacode_outputs"
    result = result.expanduser().resolve()
    result.mkdir(parents=True, exist_ok=True)
    return result


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_exact(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _files(root: Path) -> Iterator[Path]:
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in _IGNORED and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(directory) / name
            yield path


def _display_path(path: Path, workspace: Path) -> str:
    return str(path.relative_to(workspace)) if path.is_relative_to(workspace) else str(path)


def _limited(value: Any, runtime: ToolRuntime, source: str) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    limit = int(context_value(runtime.context, "output_limit_bytes", 64 * 1024))
    if limit <= 0:
        limit = 64 * 1024
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return value
    path = _output_dir(runtime) / f"{source}-{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return {
        "preview": encoded[:limit].decode("utf-8", "ignore"),
        "characters": len(text),
        "bytes": len(encoded),
        "output_file": path.name,
    }


@tool
def read_file(
    path: str, runtime: ToolRuntime[Any], offset: int = 1, limit: int = 250
) -> dict[str, Any]:
    """按从一开始的行号读取 UTF-8 文本；大文件使用 offset 与 limit 分段。"""
    if offset < 1 or not 1 <= limit <= 2000:
        raise ValueError("offset must be positive and limit must be 1..2000")
    lines = _path(runtime, path).read_text(encoding="utf-8-sig").splitlines()
    selected = lines[offset - 1 : offset - 1 + limit]
    return {
        "path": path,
        "total_lines": len(lines),
        "offset": offset,
        "content": _limited(
            "\n".join(f"{i}: {line}" for i, line in enumerate(selected, offset)), runtime, "read"
        ),
    }


@tool
def write_file(path: str, content: str, runtime: ToolRuntime[Any]) -> dict[str, Any]:
    """创建或覆盖 UTF-8 文件，并创建缺失的父目录。"""
    target = _path(runtime, path)
    with _EDIT_LOCK:
        _atomic_write(target, content)
    return {"path": path, "bytes": len(content.encode("utf-8"))}


def _replace(content: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    if not old:
        raise ValueError("old_text must not be empty")
    matches = content.count(old)
    if not matches:
        raise ValueError("old_text was not found; read the file again")
    if matches != 1 and not replace_all:
        raise ValueError(
            f"old_text matches {matches} locations; provide more context or replace_all"
        )
    return content.replace(old, new, -1 if replace_all else 1), matches


@tool
def search_replace(
    path: str, old_text: str, new_text: str, runtime: ToolRuntime[Any], replace_all: bool = False
) -> dict[str, Any]:
    """精确替换文本；有多个匹配时需显式指定 replace_all。"""
    target = _path(runtime, path)
    with _EDIT_LOCK:
        content, count = _replace(_read_exact(target), old_text, new_text, replace_all)
        _atomic_write(target, content)
    return {"path": path, "replacements": count}


@tool
def delete_file(path: str, runtime: ToolRuntime[Any], recursive: bool = False) -> dict[str, Any]:
    """删除文件或空目录；删除目录树必须显式启用 recursive。"""
    target = _path(runtime, path)
    lexical = Path(path).expanduser()
    lexical = lexical if lexical.is_absolute() else _root(runtime) / lexical
    with _EDIT_LOCK:
        if lexical.is_symlink():
            lexical.unlink()
        elif target.is_dir():
            shutil.rmtree(target) if recursive else target.rmdir()
        else:
            target.unlink()
    return {"path": path, "deleted": True}


@tool
def list_directory(path: str, runtime: ToolRuntime[Any]) -> Any:
    """列出目录直接子项及大小，可访问工作区之外。"""
    target = _path(runtime, path)
    rows = []
    for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        rows.append(
            {"name": entry.name, "directory": entry.is_dir(), "bytes": entry.stat().st_size}
        )
    return _limited(rows, runtime, "directory")


@tool
def read_output_file(
    path: str,
    runtime: ToolRuntime[Any],
    mode: Literal["head", "tail", "grep"] = "tail",
    lines: int = 100,
    pattern: str = "",
) -> Any:
    """按返回的文件名读取工具完整输出，支持头部、尾部和正则筛选。"""
    if not 1 <= lines <= 2000:
        raise ValueError("lines must be 1..2000")
    root = _output_dir(runtime)
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise PermissionError("Output path escapes the artifact directory")
    from collections import deque
    from itertools import islice

    with target.open(encoding="utf-8", errors="replace") as handle:
        if mode == "tail":
            selected = list(deque(handle, maxlen=lines))
        elif mode == "head":
            selected = list(islice(handle, lines))
        else:
            matcher = re.compile(pattern)
            selected = list(islice((line for line in handle if matcher.search(line)), lines))
    return _limited("".join(selected), runtime, "output")
