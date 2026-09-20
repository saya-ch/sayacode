"""项目说明和持久记忆文本，用于组装系统提示。"""

from __future__ import annotations

import re
from pathlib import Path

_IMPORT = re.compile(r"^@\./([^\s]+)\s*$", re.MULTILINE)
_MAX_FILE_BYTES = 128_000
_MAX_TOTAL_BYTES = 256_000


def _safe_read(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _expand(text: str, root: Path, source: Path, seen: set[Path], budget: list[int]) -> str:
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
    """从工作区祖先目录安全加载项目说明文件。"""
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
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        if target.stat().st_size:
            handle.write("\n")
        handle.write(text.strip() + "\n")


__all__ = ["append_user_memory", "load_project_instructions"]
