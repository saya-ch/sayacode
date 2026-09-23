"""读取人工维护的用户说明和项目约定文件。"""

from __future__ import annotations

import re
from pathlib import Path

_IMPORT = re.compile(r"^@\./([^\s]+)\s*$", re.MULTILINE)
_MAX_FILE_BYTES = 128_000
_MAX_TOTAL_BYTES = 256_000


def _safe_read(path: Path) -> str:
    """只读取体积可控的 UTF-8 文本。"""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _expand(text: str, root: Path, source: Path, seen: set[Path], budget: list[int]) -> str:
    """只展开当前项目根目录内的相对文件引用，并防止循环。"""
    if budget[0] <= 0:
        return ""

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


def load_project_instructions(
    workspace: str | Path, user_instructions: str | Path | None = None
) -> str:
    """收集用户说明及工作区祖先目录中的项目约定。"""
    root = Path(workspace).expanduser().resolve()
    segments: list[str] = []
    current = root
    budget = [_MAX_TOTAL_BYTES]
    while True:
        for name in ("SAYACODE.md", "CLAUDE.md"):
            path = current / name
            content = _safe_read(path)
            if content:
                budget[0] -= len(content.encode("utf-8"))
                segments.append(_expand(content, root, path, {path}, budget))
        if current.parent == current:
            break
        current = current.parent
    if user_instructions:
        content = _safe_read(Path(user_instructions).expanduser().resolve())
        if content:
            segments.insert(0, content)
    return "\n\n".join(part.strip() for part in segments if part.strip())[:_MAX_TOTAL_BYTES]


__all__ = ["load_project_instructions"]
