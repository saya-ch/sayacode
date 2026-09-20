"""发现并展开用户编写的 Markdown 斜杠命令。"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_ARGUMENT = re.compile(r"\$(ARGUMENTS|[1-9][0-9]*)")


@dataclass(frozen=True, slots=True)
class CustomCommand:
    invocation: str
    path: Path
    scope: str
    description: str
    body: str


def _read_command(path: Path, root: Path, scope: str) -> CustomCommand | None:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None
    try:
        source = resolved.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    match = _FRONTMATTER.match(source)
    description = ""
    body = source
    if match:
        for line in match.group(1).splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "description":
                description = value.strip().strip("\"'")
        body = source[match.end() :]
    relative = path.relative_to(root).with_suffix("")
    invocation = "/" + ":".join(relative.parts).lower()
    return CustomCommand(invocation, path, scope, description, body.strip())


def discover_custom_commands(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
) -> dict[str, CustomCommand]:
    """命名冲突时优先返回项目命令。"""
    workspace_root = Path(workspace).expanduser().resolve()
    state_home = (
        Path(home or os.environ.get("SAYACODE_HOME") or Path.home() / ".sayacode")
        .expanduser()
        .resolve()
    )
    roots = (
        ("project", workspace_root / ".sayacode" / "commands"),
        ("project", workspace_root / ".claude" / "commands"),
        ("user", state_home / "commands"),
        ("user", Path.home() / ".claude" / "commands"),
    )
    commands: dict[str, CustomCommand] = {}
    for scope, root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            command = _read_command(path, root, scope)
            if command is None:
                continue
            commands.setdefault(command.invocation, command)
            if ":" in command.invocation:
                commands.setdefault("/" + path.stem.lower(), command)
    return commands


def expand_custom_command(
    text: str,
    commands: dict[str, CustomCommand],
) -> tuple[CustomCommand, str] | None:
    """展开参数占位符，不执行 Markdown 内容。"""
    invocation, _, raw_arguments = text.strip().partition(" ")
    command = commands.get(invocation.lower())
    if command is None:
        return None
    try:
        arguments = shlex.split(raw_arguments, posix=True)
    except ValueError:
        arguments = raw_arguments.split()

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "ARGUMENTS":
            return raw_arguments.strip()
        index = int(token) - 1
        return arguments[index] if index < len(arguments) else ""

    return command, _ARGUMENT.sub(replace, command.body).strip()
