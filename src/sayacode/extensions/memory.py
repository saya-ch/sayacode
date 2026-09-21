"""项目说明与持久记忆文本，用于组装系统提示。

加载时从工作区向上逐层查找说明文件，并展开内部引用。
引用越界与循环引用会被静默丢弃，总长度受预算控制。
用户记忆始终排在最前，项目记忆按就近优先拼接。"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..application import SayacodeApp

_IMPORT = re.compile(r"^@\./([^\s]+)\s*$", re.MULTILINE)
_MAX_FILE_BYTES = 128_000
_MAX_TOTAL_BYTES = 256_000


def _safe_read(path: Path) -> str:
    """安全读取小文本文件，过大缺失或解码失败返回空。"""
    if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _expand(text: str, root: Path, source: Path, seen: set[Path], budget: list[int]) -> str:
    """展开文本中的文件引用，越界与重复引用直接丢弃。

    参数含原文与根目录约束。来源路径用于解析相对位置。
    已见集合防止循环展开，预算耗尽后不再展开。
    预算按字节扣减，引用内容会递归继续展开。"""
    if budget[0] <= 0:
        return text[:0]

    def replacement(match: re.Match[str]) -> str:
        candidate = (source.parent / match.group(1)).resolve()
        if candidate in seen or not candidate.is_relative_to(root):
            return ""
        seen.add(candidate)
        included = _safe_read(candidate)
        if not included:
            return ""
        budget[0] -= len(included.encode("utf-8"))
        return _expand(included, root, candidate, seen, budget)

    return _IMPORT.sub(replacement, text)


def load_project_instructions(workspace: str | Path, user_memory: str | Path | None = None) -> str:
    """从工作区向上收集说明文本并拼接返回。

    参数为工作区路径与可选用户记忆路径。返回截断后的拼接文本。
    分三步执行。先自下而上查找三类说明文件并展开引用。
    再把用户记忆插到最前，最后去空拼接并按总预算截断。
    约束是查找止于文件系统根，单文件超限会被跳过。
    坑点是预算在收集与展开中共享，大文件会挤占后继内容。"""
    root = Path(workspace).expanduser().resolve()
    segments: list[str] = []
    current = root
    budget = [_MAX_TOTAL_BYTES]
    while True:
        for name in ("SAYACODE.md", "CLAUDE.md", ".sayacode/memory.md"):
            path = current / name
            text = _safe_read(path)
            if text:
                budget[0] -= len(text.encode("utf-8"))
                segments.append(_expand(text, root, path, {path}, budget))
        if current.parent == current:
            break
        current = current.parent
    if user_memory:
        path = Path(user_memory).expanduser().resolve()
        text = _safe_read(path)
        if text:
            segments.insert(0, text)
    return "\n\n".join(part.strip() for part in segments if part.strip())[:_MAX_TOTAL_BYTES]


def append_user_memory(path: str | Path, text: str) -> None:
    """向记忆文件末尾追加一行文本。

    参数为目标路径与待写文本。无返回值。
    父目录不存在会自动创建，非空文件会先补换行。
    坑点是文本首尾空白会被清理，空行不会被写入。"""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        if target.stat().st_size:
            handle.write("\n")
        handle.write(text.strip() + "\n")


__all__ = ["append_user_memory", "load_project_instructions"]


async def _memory_command(app: SayacodeApp, args: Any) -> Any:
    """解析记忆斜杠命令并执行查看初始化与追加。

    参数为应用实例与原始参数文本。返回各动作的状态字典。
    先拆动作与范围，范围限定用户或项目，非法范围直接抛错。
    查看返回加载后字符数，初始化只建空文件，追加后清空句柄缓存。
    约束是追加必须同时给出范围与文本，缺失会提示用法。
    坑点是项目记忆固定指向工作区根下文件，不随子目录变化。"""
    tokens = shlex.split(str(args or ""))
    action = tokens[0].lower() if tokens else "status"
    scope = tokens[1].lower() if len(tokens) > 1 else "user"
    target = app.paths.memory if scope == "user" else app.workspace / "SAYACODE.md"
    if scope not in {"user", "project"}:
        raise ValueError("memory scope must be user or project")
    if action == "status":
        instructions = load_project_instructions(app.workspace, app.paths.memory)
        return {
            "user_memory": str(app.paths.memory),
            "project_memory": str(app.workspace / "SAYACODE.md"),
            "loaded_characters": len(instructions),
        }
    if action == "init":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
        return {"initialized": str(target)}
    if action == "append":
        if len(tokens) < 3:
            raise ValueError("Usage: /memory append <user|project> <text>")
        append_user_memory(target, " ".join(tokens[2:]))
        app._handles.clear()
        return {"appended": str(target)}
    raise ValueError("Usage: /memory [status|init <user|project>|append <user|project> <text>]")
