"""直接文件工具与完整输出定位，只做本机文件读写。

写用临时文件加替换保证原子性，读大结果转输出文件。
目录漫步会跳过常见大目录和符号链接，输出目录自动建好。"""

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

from ..paths import context_value, workspace_path

_IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".sayacode_outputs"}
_EDIT_LOCK = threading.RLock()


def _root(runtime: ToolRuntime) -> Path:
    """取当前会话的工作区根，越界校验由下层统一做。"""
    return workspace_path(runtime.context)


def _path(runtime: ToolRuntime, path: str) -> Path:
    """把用户给的相对或绝对路径换算成可操作路径。

    越界会直接报错，调用方不用再判。"""
    return workspace_path(runtime.context, path)


def _output_dir(runtime: ToolRuntime) -> Path:
    """取放超长输出的目录，不存在会自动建好。

    优先用上下文配置，无配置用工作区下默认目录。
    路径会被规范化，调用方直接写文件即可。"""
    value = context_value(runtime.context, "output_dir")
    result = Path(value) if value else _root(runtime) / ".sayacode_outputs"
    result = result.expanduser().resolve()
    result.mkdir(parents=True, exist_ok=True)
    return result


def _atomic_write(path: Path, content: str) -> None:
    """先写临时文件再整体替换，保证写一半崩了不留半截文件。

    父目录自动建，老文件权限会继承，临时文件收尾必删。"""
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
    """按原文读整文件，不做换行转换，供替换前取底稿用。"""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _files(root: Path) -> Iterator[Path]:
    """漫步该目录下全部文件，跳过大目录和符号链接。

    目录和文件名都按序排，保证多次跑顺序一致。"""
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
    """路径能落到工作区就显相对路径，落不到显绝对路径。

    只做展示换算，不做越界拦截。"""
    return str(path.relative_to(workspace)) if path.is_relative_to(workspace) else str(path)


def _limited(value: Any, runtime: ToolRuntime, source: str) -> Any:
    """结果超限就转存文件并只回预览，超限阈值来自上下文。

    分两步走，先算字节数，不超直接回原文。
    超了写输出目录，回预览加字数加字节加文件名。
    坑点是阈值非法会被纠正为默认值，不要传零取巧。"""
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
    """做什么，按从一开始的行号读取文本，大文件用分页读。

    参数与返回，入参是路径起始行和条数，返回路径总行数和内容。
    调用约束，起始行须为正，条数只能在一到两千之间。
    坑点是大结果会转输出文件，内容字段可能是截断指引。"""
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
    """做什么，创建或覆盖文件，并创建缺失的父目录。

    参数与返回，入参是路径和全文，返回路径和字节数。
    调用约束，写操作加全局编辑锁，并发写同一文件会排队。
    坑点是覆盖不做备份，写前要自己读好确认。"""
    target = _path(runtime, path)
    with _EDIT_LOCK:
        _atomic_write(target, content)
    return {"path": path, "bytes": len(content.encode("utf-8"))}


def _replace(content: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    """在内存里做精确替换并返回新文本和命中数。

    空旧文直接报错，找不到也报错，多命中须显式开全量替换。
    只换文本不落盘，落盘由上层加锁后做。"""
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
    """做什么，精确替换文本，有多个匹配时需显式指定全量。

    参数与返回，入参是路径旧文新文和全量开关，返回路径和命中数。
    调用约束，读改写全程加锁，旧文为空或找不到会报错。
    坑点是替换按字面来，不支持正则，改前最好先读一遍。"""
    target = _path(runtime, path)
    with _EDIT_LOCK:
        content, count = _replace(_read_exact(target), old_text, new_text, replace_all)
        _atomic_write(target, content)
    return {"path": path, "replacements": count}


@tool
def delete_file(path: str, runtime: ToolRuntime[Any], recursive: bool = False) -> dict[str, Any]:
    """做什么，删除文件或空目录，删除目录树必须显式启用递归。

    参数与返回，入参是路径和递归开关，返回路径和删除标记。
    调用约束，删目录树不开递归会报错，符号链接只删链本身。
    坑点是删除不可恢复，目录非空又不开递归会直接失败。"""
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
