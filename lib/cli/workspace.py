"""
工作区模块

包含工作区路径解析、Git 变更检查等。
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from lib.theme import (
    console,
    print_success,
    print_warning,
    confirm_action,
    SayacodeColors,
)
from lib.i18n import tr
from lib.cli.permissions import _supports_interactive_input, _safe_console_input


def resolve_launch_workspace(args, user_config=None) -> Path:
    """解析本次启动应使用的工作区。

    默认使用当前终端目录；仅当显式传入 --workspace 时才覆盖。
    """
    if getattr(args, "workspace", None):
        workspace = Path(args.workspace).expanduser().resolve()
        if not workspace.exists():
            workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    return get_workspace_path(Path.cwd())


def get_workspace_path(default_path: Optional[Path] = None) -> Path:
    """
    获取工作区路径（简化版本）

    直接提示输入，不使用复杂菜单。
    默认使用当前终端目录。
    """
    current_dir = Path.cwd().resolve()
    default_path = (default_path or current_dir).expanduser().resolve()

    if not _supports_interactive_input():
        if not default_path.exists():
            default_path.mkdir(parents=True, exist_ok=True)
        return default_path

    # 提示输入
    console.print()
    if default_path == current_dir:
        default_display = "."
    else:
        try:
            default_display = f"./{default_path.relative_to(current_dir)}"
        except ValueError:
            default_display = str(default_path)

    if default_path == current_dir:
        hint = f"[{SayacodeColors.TEXT_DIM}]{tr('workspace_prompt.current_hint')}[/]"
    else:
        hint = (
            f"[{SayacodeColors.TEXT_DIM}]"
            f"{tr('workspace_prompt.other_hint', default_display=default_display)}"
            f"[/]"
        )
    console.print(f"[{SayacodeColors.PRIMARY}]> {tr('workspace_prompt.title')}[/] {hint}")

    path_str = _safe_console_input("  > ").strip()

    if not path_str:
        # 使用默认值
        if not default_path.exists():
            default_path.mkdir(parents=True, exist_ok=True)
        return default_path

    # 处理用户输入的路径
    path = Path(path_str).expanduser().resolve()

    if not path.exists():
        if confirm_action(tr("workspace_prompt.create")):
            path.mkdir(parents=True, exist_ok=True)
            print_success(tr("workspace_prompt.created", path=path))
        else:
            return current_dir

    return path


def check_git_changes(workspace: Path) -> bool:
    """检查是否有未提交的更改"""
    git_dir = workspace / ".git"
    if not git_dir.exists():
        return False

    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def suggest_git_commit(workspace: Path):
    """建议用户提交更改"""
    if not check_git_changes(workspace):
        return

    console.print()
    print_warning(tr("git.uncommitted_changes"))

    if confirm_action(tr("git.commit_now")):
        from lib.tools.git_tools import git_add, git_commit, git_status

        # 显示状态
        status = git_status.invoke({})
        console.print(status)

        # 暂存
        git_add.invoke({"add_all": True})

        # 提交信息
        console.print(f"\n[{SayacodeColors.TEXT_DIM}]{tr('git.commit_message')}[/]")
        message = console.input("  > ").strip()

        if message:
            result = git_commit.invoke({"message": message})
            console.print(result)
