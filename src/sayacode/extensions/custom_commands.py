"""发现并展开用户编写的斜杠命令。

命令来自项目与用户两级目录，项目级优先于用户级。
文件名决定调用名，嵌套目录用分隔符连接。
展开时只做文本替换，不执行任何命令。"""

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
    """单条自定义命令，记录调用名路径与正文。

    调用名已统一小写。路径指向原始文件。
    来源区分项目级与用户级。描述来自文件头，可为空。
    正文已去掉头部并清理首尾空白。"""
    invocation: str
    path: Path
    scope: str
    description: str
    body: str


def _read_command(path: Path, root: Path, scope: str) -> CustomCommand | None:
    """读取单个命令文件，越界或损坏时返回空。

    先校验解析后路径仍在根目录内，防止符号链接逃逸。
    再读文本并分离头部描述，缺头部则全文视为正文。
    最后按相对路径生成调用名，嵌套层级保留在名称中。"""
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
    """扫描四级目录并返回调用名到命令的映射。

    参数为工作区路径与可选状态目录。返回以调用名为键的字典。
    按项目优先用户其次的固定顺序扫描，先到先得保留重名首项。
    嵌套命令会额外注册短名，便于直接调用。
    约束是不存在的目录直接跳过，不抛错。
    坑点是短名也可能冲突，同样只保留首次扫描结果。"""
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
    """匹配调用名并展开参数占位，未命中返回空。

    参数为用户输入全文与命令映射。返回命令与展开后正文。
    先按首空格切出调用名，匹配时忽略大小写。
    再解析剩余参数，解析失败回落为空格切分。
    全部参数整体替换一处，编号参数缺失时填空串。
    约束是只做文本替换，不执行命令也不校验参数个数。"""
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
