"""自定义命令归属 lib.commands 包，由 custom 模块承载。

职责是从项目级与用户级目录发现 markdown 命令，并把一次调用展开成
prompt。核心类与函数为 `CustomCommand` / `discover_custom_commands` /
`render_custom_command`，由 Agent 工具链按调用名检索调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple
import shlex


@dataclass
class CustomCommand:
    """以 markdown 为载体的自定义 slash 命令。"""

    name: str
    path: Path
    scope: str
    body: str
    description: str = ""
    namespace: str = ""

    @property
    def primary_invocation(self) -> str:
        """返回主调用名。"""
        return f"/{self.name}"

    @property
    def qualified_invocation(self) -> Optional[str]:
        """返回带命名空间的调用名。"""
        if not self.namespace:
            return None
        return f"/{self.namespace}:{self.name}"

    @property
    def invocations(self) -> tuple[str, ...]:
        """返回全部可用调用名。"""
        aliases = [self.primary_invocation]
        if self.qualified_invocation:
            aliases.append(self.qualified_invocation)
        return tuple(aliases)

    @property
    def source_label(self) -> str:
        """返回命令来源标签。"""
        if self.namespace:
            return f"{self.scope}:{self.namespace}"
        return self.scope


def _split_frontmatter(content: str) -> tuple[Dict[str, str], str]:
    """解析最简形式的类 YAML frontmatter。"""
    if not content.startswith("---\n"):
        return {}, content

    closing_marker = "\n---\n"
    end_index = content.find(closing_marker, 4)
    if end_index == -1:
        return {}, content

    raw_frontmatter = content[4:end_index]
    body = content[end_index + len(closing_marker):]
    metadata: Dict[str, str] = {}

    for line in raw_frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip().strip('"').strip("'")

    return metadata, body


def _command_roots(workspace: Path) -> list[tuple[str, Path]]:
    workspace = Path(workspace).expanduser().resolve()
    return [
        ("project", workspace / ".claude" / "commands"),
        ("user", Path.home() / ".claude" / "commands"),
    ]


def discover_custom_commands(workspace: Path) -> Dict[str, CustomCommand]:
    """从项目级与用户级作用域发现 Claude 风格的 markdown 命令。"""
    commands: Dict[str, CustomCommand] = {}

    for scope, root in _command_roots(workspace):
        if not root.exists():
            continue

        for path in sorted(root.rglob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
            except Exception:
                continue

            metadata, body = _split_frontmatter(raw)
            relative_path = path.relative_to(root)
            namespace = relative_path.parent.as_posix().replace("/", ":").strip(".")
            name = path.stem.strip().lower()

            if not name:
                continue

            command = CustomCommand(
                name=name,
                path=path,
                scope=scope,
                body=body.strip(),
                description=metadata.get("description", ""),
                namespace=namespace,
            )

            aliases = [command.primary_invocation]
            if command.qualified_invocation:
                aliases.append(command.qualified_invocation)

            for alias in aliases:
                if alias not in commands:
                    commands[alias] = command

    return commands


def list_custom_commands(workspace: Path) -> list[CustomCommand]:
    """返回去重后的已发现命令列表。"""
    deduped: Dict[tuple[str, str, str], CustomCommand] = {}
    for command in discover_custom_commands(workspace).values():
        key = (command.name, command.scope, command.namespace)
        deduped[key] = command
    return sorted(
        deduped.values(),
        key=lambda item: (item.scope != "project", item.namespace, item.name),
    )


def render_custom_command(invocation: str, workspace: Path) -> Tuple[Optional[CustomCommand], Optional[str]]:
    """把 Claude 风格 markdown 命令的一次调用展开成 prompt。"""
    raw = invocation.strip()
    if not raw.startswith("/"):
        return None, None

    name, _, argument_text = raw.partition(" ")
    commands = discover_custom_commands(workspace)
    command = commands.get(name.lower())
    if not command:
        return None, None

    try:
        args = shlex.split(argument_text, posix=True) if argument_text.strip() else []
    except ValueError:
        args = argument_text.split()

    rendered = command.body.replace("$ARGUMENTS", argument_text.strip())
    for index, value in enumerate(args, 1):
        rendered = rendered.replace(f"${index}", value)

    # 清掉未解析的编号占位符，避免把模板标记泄漏到结果里。
    for index in range(len(args) + 1, 10):
        rendered = rendered.replace(f"${index}", "")

    return command, rendered.strip()


def load_project_mcp_config(workspace: Path) -> tuple[Path, Dict]:
    """如果存在，加载 Claude 风格的项目 .mcp.json 文件。"""
    config_path = Path(workspace).expanduser().resolve() / ".mcp.json"
    if not config_path.exists():
        return config_path, {}

    import json

    try:
        return config_path, json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return config_path, {}
