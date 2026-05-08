"""Project and user memory files loaded into agent context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import re

from .paths import SayacodePaths
from .private_io import ensure_private_dir, write_private_text


PROJECT_MEMORY_NAME = "SAYACODE.md"
COMPAT_PROJECT_MEMORY_NAME = "CLAUDE.md"
USER_MEMORY_NAME = "memory.md"
MAX_MEMORY_FILE_CHARS = 12000
MAX_MEMORY_TOTAL_CHARS = 24000
MAX_IMPORT_DEPTH = 5


@dataclass(frozen=True)
class MemoryFile:
    """One loaded memory file."""

    path: Path
    label: str
    content: str
    truncated: bool = False


def sayacode_home(create: bool = False) -> Path:
    """Return the SAYACODE home directory."""
    path = SayacodePaths.resolve(create=False).home
    return ensure_private_dir(path) if create else path


def user_memory_path(create_parent: bool = False) -> Path:
    """Return the user memory file path."""
    directory = sayacode_home(create=create_parent)
    return directory / USER_MEMORY_NAME


def primary_project_memory_path(workspace: str | Path) -> Path:
    """Return the primary project memory path."""
    return Path(workspace).expanduser().resolve() / PROJECT_MEMORY_NAME


def discover_project_memory_paths(workspace: str | Path) -> list[Path]:
    """Find project memory files from workspace up to filesystem root."""
    current = Path(workspace).expanduser().resolve()
    paths: list[Path] = []
    seen: set[Path] = set()

    while True:
        for filename in (PROJECT_MEMORY_NAME, COMPAT_PROJECT_MEMORY_NAME):
            candidate = current / filename
            if candidate.exists() and candidate.is_file() and candidate not in seen:
                paths.append(candidate)
                seen.add(candidate)

        parent = current.parent
        if parent == current:
            break
        current = parent

    return paths


def load_memory_files(workspace: str | Path, include_user: bool = True) -> list[MemoryFile]:
    """Load user and project memory files for prompt injection."""
    files: list[MemoryFile] = []
    workspace_root = Path(workspace).expanduser().resolve()

    if include_user:
        user_path = user_memory_path(create_parent=False)
        if user_path.exists() and user_path.is_file():
            files.append(_load_memory_file(
                user_path,
                "User memory",
                allowed_import_root=user_path.parent,
            ))

    for path in discover_project_memory_paths(workspace_root):
        label = "Project memory" if path.name == PROJECT_MEMORY_NAME else "Compatibility memory"
        files.append(_load_memory_file(
            path,
            label,
            allowed_import_root=workspace_root,
        ))

    return _limit_total_memory(files)


def render_memory_for_prompt(workspace: str | Path) -> str:
    """Render loaded memory files for the system prompt."""
    files = load_memory_files(workspace)
    if not files:
        return ""

    lines = [
        "## Persistent Memory",
        "Use these instructions as durable user/project preferences. If they conflict with the user's current request, follow the current request and mention the conflict when relevant.",
        "",
    ]
    for item in files:
        lines.extend([
            f"### {item.label}: {item.path}",
            item.content.strip() or "(empty)",
        ])
        if item.truncated:
            lines.append("[memory file truncated]")
        lines.append("")

    return "\n".join(lines).strip()


def render_memory_status(workspace: str | Path) -> str:
    """Render a CLI-friendly memory file status."""
    files = load_memory_files(workspace)
    project_path = primary_project_memory_path(workspace)
    user_path = user_memory_path(create_parent=False)

    lines = [
        "Memory Files",
        f"User: {user_path} ({'present' if user_path.exists() else 'missing'})",
        f"Project: {project_path} ({'present' if project_path.exists() else 'missing'})",
    ]
    discovered = discover_project_memory_paths(workspace)
    if discovered:
        lines.append("")
        lines.append("Loaded:")
        for item in files:
            size = len(item.content)
            suffix = " truncated" if item.truncated else ""
            lines.append(f"  - {item.label}: {item.path} ({size} chars{suffix})")
    else:
        lines.append("")
        lines.append("No project memory found. Run /memory init to create SAYACODE.md.")

    return "\n".join(lines)


def initialize_project_memory(workspace: str | Path) -> Path:
    """Create a starter project memory file when missing."""
    path = primary_project_memory_path(workspace)
    if path.exists():
        return path

    path.write_text(
        "\n".join(
            [
                "# SAYACODE Project Memory",
                "",
                "## Project Conventions",
                "- Add project-specific coding standards here.",
                "- Add common test, lint, and build commands here.",
                "",
                "## Architecture Notes",
                "- Add stable architecture facts here.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def append_project_memory(workspace: str | Path, text: str) -> Path:
    """Append one memory entry to the primary project memory."""
    path = initialize_project_memory(workspace)
    _append_text(path, text)
    return path


def append_user_memory(text: str) -> Path:
    """Append one memory entry to the user memory."""
    path = user_memory_path(create_parent=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else "# SAYACODE User Memory\n"
    content = _with_entry(existing, text)
    write_private_text(path, content)
    return path


def _load_memory_file(
    path: Path,
    label: str,
    *,
    allowed_import_root: Optional[Path] = None,
) -> MemoryFile:
    text = _read_with_imports(
        path,
        depth=0,
        seen=set(),
        allowed_import_root=allowed_import_root,
    )
    truncated = len(text) > MAX_MEMORY_FILE_CHARS
    if truncated:
        text = text[:MAX_MEMORY_FILE_CHARS]
    return MemoryFile(path=path, label=label, content=text, truncated=truncated)


def _limit_total_memory(files: list[MemoryFile]) -> list[MemoryFile]:
    remaining = MAX_MEMORY_TOTAL_CHARS
    limited: list[MemoryFile] = []
    for item in files:
        if remaining <= 0:
            break
        content = item.content
        truncated = item.truncated
        if len(content) > remaining:
            content = content[:remaining]
            truncated = True
        limited.append(MemoryFile(item.path, item.label, content, truncated))
        remaining -= len(content)
    return limited


def _read_with_imports(
    path: Path,
    depth: int,
    seen: set[Path],
    *,
    allowed_import_root: Optional[Path] = None,
) -> str:
    resolved = path.expanduser().resolve()
    if resolved in seen or depth > MAX_IMPORT_DEPTH:
        return ""
    seen.add(resolved)

    try:
        text = resolved.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    lines: list[str] = []
    in_code_block = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            lines.append(line)
            continue

        if not in_code_block:
            import_path = _extract_import_path(line)
            if import_path:
                imported = _resolve_import_path(import_path, resolved.parent)
                if imported and imported.exists() and imported.is_file():
                    if not _memory_import_allowed(imported, allowed_import_root):
                        lines.append("<!-- blocked memory import outside trusted workspace -->")
                        continue
                    lines.append(f"\n<!-- imported: {imported} -->")
                    lines.append(_read_with_imports(
                        imported,
                        depth + 1,
                        seen,
                        allowed_import_root=allowed_import_root,
                    ))
                    continue

        lines.append(line)

    return "\n".join(lines).strip()


def _extract_import_path(line: str) -> Optional[str]:
    stripped = line.strip()
    if "`@" in stripped:
        return None
    match = re.search(r"(?<![`])@([~./A-Za-z0-9_:\\-][^\s`<>]*)", stripped)
    if not match:
        return None
    return match.group(1).rstrip(".,;)")


def _resolve_import_path(value: str, base_dir: Path) -> Optional[Path]:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        return path.resolve()
    except OSError:
        return None


def _memory_import_allowed(path: Path, allowed_root: Optional[Path]) -> bool:
    """Keep @ imports inside the trusted root and away from credential files."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False

    if _looks_like_sensitive_memory_import(resolved):
        return False

    if allowed_root is None:
        return True

    try:
        resolved.relative_to(Path(allowed_root).expanduser().resolve())
    except ValueError:
        return False
    return True


def _looks_like_sensitive_memory_import(path: Path) -> bool:
    normalized = path.as_posix().lower()
    sensitive_patterns = (
        r"(?:^|/)\.ssh(?:/|$)",
        r"(?:^|/)\.env(?:\.(?!(?:example|sample|template|dist)$)[^/]*)?$",
        r"(?:^|/)(?!(?:example|sample|template|dist)\.env$)[^/]*\.env$",
        r"(?:^|/)(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:\.pub)?$",
        r"\.(?:pem|p12|pfx|key)$",
        r"(?:^|/)\.(?:npmrc|pypirc|netrc)$",
        r"(?:^|/)(?:credentials|secrets?|tokens?)(?:\.[^/]*)?$",
    )
    return any(re.search(pattern, normalized) for pattern in sensitive_patterns)


def _append_text(path: Path, text: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(_with_entry(existing, text), encoding="utf-8")


def _with_entry(existing: str, text: str) -> str:
    entry = str(text or "").strip()
    if not entry:
        return existing if existing.endswith("\n") else existing + "\n"
    prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
    return f"{prefix}- {entry}\n"


__all__ = [
    "MemoryFile",
    "append_project_memory",
    "append_user_memory",
    "discover_project_memory_paths",
    "initialize_project_memory",
    "load_memory_files",
    "primary_project_memory_path",
    "render_memory_for_prompt",
    "render_memory_status",
    "sayacode_home",
    "user_memory_path",
]
