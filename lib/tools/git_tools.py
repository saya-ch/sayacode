"""
Git 集成工具

提供 Git 操作功能，包括：
- git status: 查看工作区状态
- git diff: 查看文件修改
- git commit: 提交更改
- git log: 查看提交历史
- git branch: 查看分支
- git checkout: 切换分支
- git add: 暂存文件

所有命令通过 subprocess 执行，包含错误处理和安全检查。
"""

from contextvars import ContextVar, Token
from pathlib import Path
from typing import Optional, List, Tuple
from langchain_core.tools import tool
import subprocess
import re

# 导入安全检查模块，统一校验路径风险。
from ._process import build_process_env, popen_platform_kwargs, terminate_process_tree
from .safety import sanitize_path
from ..core.permissions import enforce_tool_permission


# 维护默认工作区，供执行前校验。
DEFAULT_WORKSPACE = Path.cwd().resolve()
_WORKSPACE_CONTEXT: ContextVar[Path | None] = ContextVar("sayacode_git_tools_workspace", default=None)

GIT_TIMEOUT = 30
GIT_TERMINATION_GRACE_SECONDS = 2


def set_default_workspace(workspace: str | Path) -> Path:
    """设置 Git 工具默认工作区。"""
    global DEFAULT_WORKSPACE
    DEFAULT_WORKSPACE = Path(workspace).expanduser().resolve()
    return DEFAULT_WORKSPACE


def get_default_workspace() -> Path:
    """返回 Git 工具默认工作区。"""
    return _WORKSPACE_CONTEXT.get() or DEFAULT_WORKSPACE


def use_workspace(workspace: str | Path) -> Token[Path | None]:
    """为当前上下文临时把 Git 工具绑定到某个工作区。"""
    return _WORKSPACE_CONTEXT.set(Path(workspace).expanduser().resolve())


def reset_workspace(token: Token[Path | None]) -> None:
    """恢复先前上下文局部的 Git 工具工作区。"""
    _WORKSPACE_CONTEXT.reset(token)


def _resolve_git_workspace(cwd: Optional[str] = None) -> Path:
    """将 Git 工作目录限制在默认工作区内。"""
    workspace = get_default_workspace()
    if cwd:
        return sanitize_path(cwd, base_dir=workspace)
    return workspace


# 提供 Git 命令执行与校验辅助函数。

def _run_git_command(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = GIT_TIMEOUT
) -> Tuple[str, str, int]:
    """
    执行 Git 命令
    
    参数:
        args: 命令参数列表
        cwd: 工作目录
        timeout: 超时时间
        
    返回:
        (stdout, stderr, returncode)
    """
    if cwd is None:
        cwd = get_default_workspace()
    
    env = build_process_env()
    env.update({"GIT_ASKPASS": "", "SSH_ASKPASS": ""})

    try:
        process = subprocess.Popen(
            ['git'] + args,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            **popen_platform_kwargs(),
        )

        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return (stdout or "", stderr or "", int(process.returncode or 0))
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    # 忽略进程终止失败，继续返回超时结果。
                    pass
                stdout, stderr = process.communicate()
            timeout_message = f"⏱️ Git 命令执行超时（{timeout}秒），已终止进程树"
            stderr = (stderr or "").rstrip()
            stderr = f"{stderr}\n{timeout_message}" if stderr else timeout_message
            return (stdout or "", stderr, 124)

    except FileNotFoundError:
        return ("", "❌ Git 未安装或不在 PATH 中", 127)
    except Exception as e:
        return ("", f"❌ 执行 Git 命令出错: {str(e)}", 1)


def _format_git_output(output: str, title: str = "") -> str:
    """格式化 Git 输出"""
    lines = []
    
    if title:
        lines.append(f"\n{'=' * 50}")
        lines.append(f" {title}")
        lines.append(f"{'=' * 50}\n")
    
    lines.append(output)
    
    return "\n".join(lines)


def _is_git_repo(cwd: Path) -> bool:
    """检查目录是否是 Git 仓库（支持 worktree/bare：用 rev-parse 判定）。

    直调 subprocess 而不经 _run_git_command，避免单测 mock 后者时误判。
    """
    try:
        completed = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            cwd=str(cwd),
            env=build_process_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return completed.returncode == 0
    except Exception:
        return False


def _has_worktree_changes(cwd: Path) -> bool:
    """检查工作区或暂存区是否存在修改。"""
    stdout, _, returncode = _run_git_command(['status', '--porcelain'], cwd=cwd, timeout=10)
    return returncode == 0 and bool(stdout.strip())


def _validate_git_ref_name(ref_name: str, label: str = "ref") -> Optional[str]:
    """做基础 ref 参数校验，防止把分支名误当 Git 选项。"""
    value = str(ref_name or "").strip()
    if not value:
        return f"{label} 不能为空"
    if value.startswith("-"):
        return f"{label} 不能以 '-' 开头"
    if any(char in value for char in ("\r", "\n", "\x00")):
        return f"{label} 不能包含控制字符"
    return None


# 暴露 LangChain Git 操作工具。

@tool
def git_status(cwd: Optional[str] = None) -> str:
    """
    查看 Git 工作区状态。
    
    参数:
        cwd: Git 仓库根目录（可选，默认当前目录）
    
    返回:
        工作区状态信息
    """
    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 执行 git status 查询，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(['status'], cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 执行 git status 失败:\n{stderr}"
    
    return f"📊 Git 工作区状态:\n\n{stdout}"


@tool
def git_diff(file_path: Optional[str] = None, cwd: Optional[str] = None) -> str:
    """
    查看文件修改（git diff）。
    
    参数:
        file_path: 文件路径（可选，查看所有修改填 None）
        cwd: Git 仓库根目录（可选）
    
    返回:
        文件修改内容
    """
    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 构建 Git 命令参数，准备执行。
    args = ['diff']
    if file_path:
        if any(char in str(file_path) for char in ("\r", "\n", "\x00")):
            return "⚠️ 文件路径不能包含控制字符"
        try:
            safe_path = sanitize_path(str(file_path), base_dir=work_dir)
            try:
                rel = safe_path.relative_to(work_dir)
                args.append('--')
                args.append(rel.as_posix())
            except ValueError:
                args.append('--')
                args.append(str(safe_path))
        except ValueError as e:
            return f"⚠️ 文件路径不安全: {e}"
    
    # 执行 git diff 查询，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 执行 git diff 失败:\n{stderr}"
    
    if not stdout:
        return "✅ 没有未提交的修改"
    
    return f"📝 文件修改 (git diff):\n\n{stdout}"


@tool
def git_log(n: int = 10, cwd: Optional[str] = None) -> str:
    """
    查看 Git 提交历史。
    
    参数:
        n: 显示最近 N 条提交记录（默认10条）
        cwd: Git 仓库根目录（可选）
    
    返回:
        提交历史
    """
    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(n, 100))

    # 执行 git log 查询，失败则返回错误。
    args = ['log', f'-{n}', '--oneline', '--graph', '--decorate', '--all']
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 执行 git log 失败:\n{stderr}"
    
    if not stdout:
        return "📜 提交历史为空"
    
    return f"📜 最近 {n} 条提交记录:\n\n{stdout}"


@tool
def git_branch(cwd: Optional[str] = None) -> str:
    """
    查看 Git 分支。
    
    参数:
        cwd: Git 仓库根目录（可选）
    
    返回:
        分支列表
    """
    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 执行 git branch 查询，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(
        ['branch', '-a', '-v'],
        cwd=work_dir
    )
    
    if returncode != 0:
        return f"❌ 执行 git branch 失败:\n{stderr}"
    
    # 格式化分支输出，标注当前分支。
    lines = ["🌿 Git 分支:\n"]
    
    for line in stdout.split('\n'):
        if line.startswith('*'):
            lines.append(f"  * {line[1:].strip()} (当前分支)")
        elif line.strip():
            lines.append(f"    {line.strip()}")
    
    return "\n".join(lines)


@tool
def git_checkout(branch: str, cwd: Optional[str] = None, create_new: bool = False) -> str:
    """
    切换 Git 分支。
    
    参数:
        branch: 要切换的分支名
        cwd: Git 仓库根目录（可选）
        create_new: 是否创建新分支（-b 选项）
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "git_checkout",
        {"branch": branch, "cwd": cwd or "", "create_new": create_new},
    )
    if permission_error:
        return permission_error

    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"

    ref_error = _validate_git_ref_name(branch, "分支名")
    if ref_error:
        return f"⚠️ {ref_error}"
    
    # 禁止覆盖未提交修改，有变更则直接拒绝。
    if _has_worktree_changes(work_dir):
        return (
            "⚠️ 工作区有未提交的修改，请先提交或stash\n"
            "建议操作:\n"
            "  1. git add 和 git commit 提交修改\n"
            "  2. 或使用 git stash 暂存修改"
        )
    
    # 构建切换分支命令，准备执行。
    args = ['checkout']
    if create_new:
        args.append('-b')
    args.append(branch)
    
    # 执行 git checkout 切换，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 切换分支失败:\n{stderr}"
    
    action = "创建并切换到" if create_new else "切换到"
    return f"✅ {action}分支 '{branch}'"


@tool
def git_add(
    files: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    add_all: bool = False,
) -> str:
    """
    暂存文件到 Git 暂存区。
    
    参数:
        files: 要暂存的文件列表（可选，add_all=True 时忽略）
        cwd: Git 仓库根目录（可选）
        add_all: 是否暂存所有修改
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "git_add",
        {"files": files or [], "cwd": cwd or "", "add_all": add_all},
    )
    if permission_error:
        return permission_error

    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 构建暂存命令，准备执行（路径过 sanitize_path 约束在 work_dir 内）。
    args = ['add']
    if add_all:
        args.append('.')
    elif files:
        sanitized: List[str] = []
        for file_path in files:
            if any(char in str(file_path) for char in ("\r", "\n", "\x00")):
                return "⚠️ 文件路径不能包含控制字符"
            try:
                safe_path = sanitize_path(str(file_path), base_dir=work_dir)
                try:
                    sanitized.append(safe_path.relative_to(work_dir).as_posix())
                except ValueError:
                    sanitized.append(str(safe_path))
            except ValueError as e:
                return f"⚠️ 文件路径不安全: {e}"
        args.append('--')
        args.extend(sanitized)
    else:
        return "⚠️ 请指定要暂存的文件或使用 add_all=True"
    
    # 执行 git add 暂存，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ git add 失败:\n{stderr}"
    
    if add_all:
        return "✅ 已暂存所有修改"
    else:
        assert files is not None
        return f"✅ 已暂存文件: {', '.join(files)}"


@tool
def git_commit(message: str, cwd: Optional[str] = None, amend: bool = False) -> str:
    """
    提交暂存区的修改。
    
    参数:
        message: 提交信息
        cwd: Git 仓库根目录（可选）
        amend: 是否修改上次提交（--amend）
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "git_commit",
        {"message": message, "cwd": cwd or "", "amend": amend},
    )
    if permission_error:
        return permission_error

    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 检查是否有暂存内容：git diff --cached --quiet（0=无暂存，1=有暂存，不依赖英文输出）。
    _, _, staged_rc = _run_git_command(['diff', '--cached', '--quiet'], cwd=work_dir)
    if staged_rc == 0:
        return "⚠️ 没有暂存的内容，请先使用 git_add 暂存文件"
    
    # 检查提交信息是否为空，为空则直接拒绝。
    if not message or not message.strip():
        return "⚠️ 提交信息不能为空"
    
    # 构建提交命令，准备执行。
    args = ['commit']
    if amend:
        args.append('--amend')
    args.extend(['-m', message])
    
    # 执行 git commit 提交，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 提交失败:\n{stderr}"
    
    # 解析提交输出，提取提交哈希展示。
    commit_match = re.search(r'\[([^\s]+)', stdout)
    if commit_match:
        commit_hash = commit_match.group(1)[:8]
        return f"✅ 提交成功!\n📝 提交哈希: {commit_hash}\n📋 提交信息: {message}"
    
    return f"✅ 提交成功!\n📋 提交信息: {message}"


@tool
def git_stash(message: Optional[str] = None, pop: bool = False, cwd: Optional[str] = None) -> str:
    """
    暂存工作区修改（git stash）。
    
    参数:
        message: 暂存信息（可选）
        pop: 是否恢复暂存并删除（git stash pop）
        cwd: Git 仓库根目录（可选）
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "git_stash",
        {"message": message or "", "pop": pop, "cwd": cwd or ""},
    )
    if permission_error:
        return permission_error

    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 构建暂存命令，准备执行。
    args = ['stash']
    if pop:
        args.append('pop')
    elif message:
        args.extend(['push', '-m', message])
    else:
        args.append('push')
    
    # 执行 git stash 暂存，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ git stash 失败:\n{stderr}"
    
    if pop:
        return "✅ 已恢复暂存的修改并删除 stash"
    else:
        return f"✅ 已暂存当前修改\n📋 {message or '未提供说明'}"


@tool
def git_pull(cwd: Optional[str] = None, rebase: bool = False) -> str:
    """
    拉取远程更新。
    
    参数:
        cwd: Git 仓库根目录（可选）
        rebase: 是否使用 rebase 模式
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "git_pull",
        {"cwd": cwd or "", "rebase": rebase},
    )
    if permission_error:
        return permission_error

    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 检查是否有未提交修改，有则直接拒绝。
    if _has_worktree_changes(work_dir):
        return "⚠️ 工作区有未提交的修改，请先提交或stash"
    
    # 构建拉取命令，准备执行。
    args = ['pull']
    if rebase:
        args.append('--rebase')
    
    # 执行 git pull 拉取，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 拉取失败:\n{stderr}"
    
    return f"✅ 已拉取远程更新\n\n{stdout or '已是最新'}"


@tool
def git_push(cwd: Optional[str] = None, set_upstream: bool = False) -> str:
    """
    推送到远程仓库。
    
    参数:
        cwd: Git 仓库根目录（可选）
        set_upstream: 是否设置上游分支
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "git_push",
        {"cwd": cwd or "", "set_upstream": set_upstream},
    )
    if permission_error:
        return permission_error

    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 构建推送命令，准备执行。
    args = ['push']
    if set_upstream:
        args.extend(['-u', 'origin', 'HEAD'])
    
    # 执行 git push 推送，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(args, cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 推送失败:\n{stderr}"
    
    return f"✅ 已推送到远程仓库\n\n{stdout or '推送成功'}"


@tool
def git_remote(cwd: Optional[str] = None) -> str:
    """
    查看远程仓库信息。
    
    参数:
        cwd: Git 仓库根目录（可选）
    
    返回:
        远程仓库信息
    """
    try:
        work_dir = _resolve_git_workspace(cwd)
    except ValueError as e:
        return f"⚠️ 工作目录不安全: {e}"
    
    # 检查目标是否为 Git 仓库，缺失则直接返回。
    if not _is_git_repo(work_dir):
        return f"❌ 目录不是 Git 仓库: {work_dir}"
    
    # 执行 git remote 查询，失败则返回错误。
    stdout, stderr, returncode = _run_git_command(['remote', '-v'], cwd=work_dir)
    
    if returncode != 0:
        return f"❌ 获取远程仓库信息失败:\n{stderr}"
    
    if not stdout.strip():
        return "⚠️ 没有配置远程仓库"
    
    return f"🌐 远程仓库:\n\n{stdout}"


# 导出公共 Git 工具。

__all__ = [
    'set_default_workspace',
    'get_default_workspace',
    'git_status',
    'git_diff',
    'git_log',
    'git_branch',
    'git_checkout',
    'git_add',
    'git_commit',
    'git_stash',
    'git_pull',
    'git_push',
    'git_remote',
]
