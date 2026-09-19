"""
工作区模块。

负责工作区路径解析与 Git 变更检查，核心函数为 resolve_launch_workspace、
get_workspace_path 与 suggest_git_commit，供 lib.cli.main 启动与退出链调用。
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from lib.cli.theme import (
    console,
    print_success,
    print_warning,
    confirm_action,
    SayacodeColors,
)
from lib.i18n import tr
from lib.cli.permissions import _supports_interactive_input, _safe_console_input


def _remembered_workspace(user_config) -> Optional[Path]:
    """读取用户配置里记住的工作区，缺失或无效返回空。"""
    if user_config is None:
        return None

    raw = (
        user_config.get("workspace")
        if isinstance(user_config, dict)
        else getattr(user_config, "workspace", None)
    )
    if not raw:
        return None

    try:
        return Path(str(raw)).expanduser().resolve()
    except (OSError, ValueError):
        return None


def resolve_launch_workspace(args, user_config=None) -> Path:
    """解析本次启动应使用的工作区。

    优先级为显式参数，记住的工作区，交互询问。
    记住的工作区仅当等于当前目录时直接采用，此时再问一次纯粹是摩擦。
    记住的工作区不等于当前目录时不静默切换，用户刚切换目录是强意图信号，
    静默换目录比多问一句更糟。
    """
    if getattr(args, "workspace", None):
        workspace = Path(args.workspace).expanduser().resolve()
        if not workspace.exists():
            workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    current = Path.cwd().resolve()
    if _remembered_workspace(user_config) == current:
        return current

    return get_workspace_path(current)


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

    # 提示用户输入路径。
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
        # 采用默认值创建并返回。
        if not default_path.exists():
            default_path.mkdir(parents=True, exist_ok=True)
        return default_path

    # 处理用户输入的路径。
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
        # 探测外部 git：环境异常一律按无更改处理。
        return False


def suggest_git_commit(workspace: Path):
    """建议用户提交更改。

    非交互输入下直接返回，本函数会发起提问，管道残留内容会被当成确认答案吃掉。
    判据放在提问点自己身上，任何调用方都受保护，也可以直接测。
    """
    if not _supports_interactive_input():
        return

    if not check_git_changes(workspace):
        return

    console.print()
    print_warning(tr("git.uncommitted_changes"))

    if confirm_action(tr("git.commit_now")):
        from lib.tools.git_tools import git_add, git_commit, git_status

        # 显示当前 Git 状态。
        status = git_status.invoke({})
        console.print(status)

        # 暂存全部变更。
        git_add.invoke({"add_all": True})

        # 读取用户输入的提交信息。
        console.print(f"\n[{SayacodeColors.TEXT_DIM}]{tr('git.commit_message')}[/]")
        message = console.input("  > ").strip()

        if message:
            result = git_commit.invoke({"message": message})
            console.print(result)
