"""
Shell 命令执行工具

提供安全的 Shell 命令执行功能，包括：
- 命令执行（subprocess）
- 命令安全性检查
- 超时控制
- 非交互 stdin 控制
- 超时后清理子进程树
- 危险命令检测

注意：所有命令都在工作目录范围内执行，不允许访问系统目录。
"""

from contextvars import ContextVar, Token
from pathlib import Path, PurePosixPath
from typing import Any, Tuple, Dict, Optional, List
from langchain_core.tools import tool
import subprocess
import shlex
import re
import sys
import os
from urllib.parse import urlsplit, urlunsplit

# 导入安全检查模块
from ._process import build_process_env, popen_platform_kwargs, terminate_process_tree
from .safety import check_command_danger, sanitize_path
from ..core.permissions import enforce_tool_permission


# ==============================================================================
# 常量定义
# ==============================================================================

# 默认工作目录
DEFAULT_WORKSPACE = Path.cwd().resolve()
_WORKSPACE_CONTEXT: ContextVar[Path | None] = ContextVar("sayacode_shell_tools_workspace", default=None)

# 默认超时时间（秒）
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
TERMINATION_GRACE_SECONDS = 2

# 最大输出长度（字符）
MAX_OUTPUT_LENGTH = 10000

# 最大输出文件保留数
MAX_OUTPUT_FILES = 50

# 最大 stdin 输入长度。Agent 工具不是交互式终端，只接受一次性输入负载。
MAX_STDIN_LENGTH = 64000

# 输出存储目录（相对于工作区）
OUTPUT_DIR_NAME = ".sayacode_outputs"


def set_default_workspace(workspace: str | Path) -> Path:
    """设置 Shell 工具默认工作区。"""
    global DEFAULT_WORKSPACE
    DEFAULT_WORKSPACE = Path(workspace).expanduser().resolve()
    return DEFAULT_WORKSPACE


def get_default_workspace() -> Path:
    """获取 Shell 工具默认工作区。"""
    return _WORKSPACE_CONTEXT.get() or DEFAULT_WORKSPACE


def use_workspace(workspace: str | Path) -> Token[Path | None]:
    """Temporarily bind shell tools to a workspace for the current context."""
    return _WORKSPACE_CONTEXT.set(Path(workspace).expanduser().resolve())


def reset_workspace(token: Token[Path | None]) -> None:
    """Restore the previous context-local shell tools workspace."""
    _WORKSPACE_CONTEXT.reset(token)


def _get_output_dir() -> Path:
    """获取输出存储目录，确保存在。"""
    output_dir = get_default_workspace() / OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _cleanup_old_outputs() -> None:
    """清理超出数量限制的旧输出文件。"""
    output_dir = _get_output_dir()
    files = sorted(output_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[MAX_OUTPUT_FILES:]:
        try:
            f.unlink()
        except OSError:
            pass


def _save_output_to_file(stdout: str, stderr: str, command: str, label: str) -> Optional[Path]:
    """将完整输出保存到文件，返回文件路径。"""
    import hashlib
    from datetime import datetime, timezone

    cmd_hash = hashlib.md5(command.encode()).hexdigest()[:8]
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    filename = f"{label}_{timestamp}_{cmd_hash}.out"
    filepath = _get_output_dir() / filename

    try:
        content = (
            f"# 命令: {command}\n"
            f"# 时间: {datetime.now(timezone.utc).isoformat()}\n"
            f"# 工作区: {get_default_workspace()}\n"
            f"{'=' * 60}\n"
            f"# STDOUT ({len(stdout)} 字符):\n"
            f"{stdout}\n"
            f"{'=' * 60}\n"
            f"# STDERR ({len(stderr)} 字符):\n"
            f"{stderr}\n"
        )
        filepath.write_text(content, encoding="utf-8")
        _cleanup_old_outputs()
        return filepath
    except OSError:
        return None


def _build_truncation_summary(output: str, label: str, filepath: Any, mode: str = "tail") -> str:
    """为截断的输出构建智能摘要（默认显示最后部分）。"""
    if not output:
        return ""

    lines = output.rstrip("\n").split("\n")
    last_lines = "\n".join(lines[-30:]) if len(lines) > 30 else output

    path_str = str(filepath) if filepath else None
    name_str = Path(str(filepath)).name if filepath else None

    summary_parts = [f"📄 {label} 过长，已保存至: {path_str}" if filepath else f"📄 {label} 过长，已截断"]
    summary_parts.append(f"   总行数: {len(lines)}, 总字符: {len(output)}")
    summary_parts.append(f"   显示内容: 最后 {min(30, len(lines))} 行 / {len(last_lines)} 字符")
    summary_parts.append("")
    summary_parts.append(last_lines)
    summary_parts.append("")
    summary_parts.append("💡 提示: 使用 read_output_file 工具读取完整输出，支持:")
    summary_parts.append(f"   - read_output_file(path='{name_str}') 读取全部")
    summary_parts.append(f"   - read_output_file(path='{name_str}', tail=100) 读取后100行")
    summary_parts.append(f"   - read_output_file(path='{name_str}', grep='error') 搜索关键词")

    return "\n".join(summary_parts)


def _resolve_work_dir(cwd: Optional[str] = None) -> Path:
    """将工作目录限制在默认工作区内。"""
    workspace = get_default_workspace()
    if cwd:
        return sanitize_path(cwd, base_dir=workspace)
    return workspace


def _resolve_output_file_path(path: str) -> Path:
    """Resolve a saved command-output path inside the output directory only."""
    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("输出文件路径不能为空")

    normalized = raw_path.replace("\\", "/")
    if Path(raw_path).is_absolute() or re.match(r"^[a-zA-Z]:", normalized):
        raise ValueError("输出文件路径不能使用绝对路径")
    if normalized.startswith("~"):
        raise ValueError("输出文件路径不能使用用户主目录展开")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("输出文件路径不能包含 .. 路径段")

    output_dir = _get_output_dir().resolve()
    candidate = (output_dir / raw_path).resolve()
    try:
        candidate.relative_to(output_dir)
    except ValueError as exc:
        raise ValueError("输出文件路径必须位于命令输出目录内") from exc
    return candidate


def _coerce_line_limit(value: Optional[int], name: str) -> Optional[int]:
    """Normalize optional head/tail line limits."""
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if normalized < 0:
        raise ValueError(f"{name} 不能为负数")
    return normalized


# ==============================================================================
# 安全检查函数
# ==============================================================================

def check_command_safety(command: str) -> Dict[str, Any]:
    """
    检查命令安全性
    
    Args:
        command: 要检查的命令
        
    Returns:
        包含检查结果的字典:
        - is_safe: 命令是否安全
        - is_dangerous: 命令是否危险
        - reason: 原因描述
        - severity: 危险等级 (normal/warning/danger)
    """
    # 基本检查
    if not command or not command.strip():
        return {
            'is_safe': False,
            'is_dangerous': True,
            'reason': '空命令',
            'severity': 'danger'
        }
    
    # 使用安全模块检查
    is_safe, reason = check_command_danger(command)
    
    if not is_safe:
        return {
            'is_safe': False,
            'is_dangerous': True,
            'reason': reason,
            'severity': 'danger'
        }
    
    # 额外检查
    command_lower = command.lower()
    
    # 检查是否有危险操作
    danger_keywords = [
        'fork', 'bomb', 'eval', 'exec',
        'shutdown', 'reboot', 'halt',
        'init ', 'killall', 'pkill -9',
    ]
    
    for keyword in danger_keywords:
        if keyword in command_lower:
            return {
                'is_safe': False,
                'is_dangerous': True,
                'reason': f'检测到危险关键词: {keyword}',
                'severity': 'danger'
            }
    
    # 检查是否有修改系统文件的操作
    system_keywords = [
        '/etc/passwd', '/etc/shadow', '/etc/sudoers',
        '/etc/fstab', '/etc/hosts',
    ]
    
    for keyword in system_keywords:
        if keyword in command:
            return {
                'is_safe': False,
                'is_dangerous': True,
                'reason': f'检测到修改系统文件: {keyword}',
                'severity': 'danger'
            }
    
    return {
        'is_safe': True,
        'is_dangerous': False,
        'reason': '命令安全',
        'severity': 'normal'
    }


def sanitize_command(command: str) -> str:
    """
    清理命令中的危险字符
    
    Args:
        command: 原始命令
        
    Returns:
        清理后的命令
    """
    # 移除危险字符序列
    dangerous_patterns = [
        r'\$\([^)]+\)',  # 命令替换
        r'`[^`]+`',       # 反引号替换
        r';\s*rm\s+',
        r'&&\s*rm\s+',
        r'\|\s*sh',
        r'>\s*/dev/',
        r'2>\s*/dev/',
    ]
    
    result = command
    for pattern in dangerous_patterns:
        result = re.sub(pattern, '', result)
    
    return result


# ==============================================================================
# 命令执行函数
# ==============================================================================

def _coerce_timeout(timeout: Any) -> int:
    """将外部传入的 timeout 规范化到允许范围。"""
    try:
        normalized = int(timeout)
    except (TypeError, ValueError):
        normalized = DEFAULT_TIMEOUT

    if normalized <= 0:
        normalized = DEFAULT_TIMEOUT
    return min(normalized, MAX_TIMEOUT)


def _normalize_input_text(input_text: Optional[str]) -> Optional[str]:
    """校验 stdin 一次性输入，None 表示不连接 stdin。"""
    if input_text is None:
        return None

    normalized = str(input_text)
    if len(normalized) > MAX_STDIN_LENGTH:
        raise ValueError(f"stdin 输入过长，最多允许 {MAX_STDIN_LENGTH} 字符")
    return normalized


def _build_process_args(command: str, shell: bool) -> tuple[List[str] | str, bool]:
    """
    构建 subprocess 参数。

    Windows 交给系统 shell 解析整条命令，避免 Python/PowerShell 引号被
    list2cmdline 或 shlex 拆坏；POSIX 默认按参数执行，需要 shell 语义时走
    /bin/sh -c。
    """
    if sys.platform.startswith("win"):
        return command, True

    if shell:
        return ["/bin/sh", "-c", command], False

    try:
        return shlex.split(command), False
    except ValueError:
        return ["/bin/sh", "-c", command], False


TERMINATION_GRACE_SECONDS = 2


def _truncate_output(output: Optional[str], label: str) -> str:
    """限制工具输出体积，避免把上下文撑爆。"""
    if not output:
        return ""

    if len(output) <= MAX_OUTPUT_LENGTH:
        return output

    omitted = len(output) - MAX_OUTPUT_LENGTH
    return output[:MAX_OUTPUT_LENGTH] + f"\n... [{label}已截断，超出 {omitted} 字符]"


def _mask_env_value(key: str, value: str) -> str:
    """隐藏环境变量中的凭据和 URL 用户信息。"""
    sensitive_markers = (
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASS",
        "CREDENTIAL",
        "AUTH",
        "COOKIE",
    )
    normalized_key = key.upper()
    if any(marker in normalized_key for marker in sensitive_markers):
        return "***"

    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            sanitized = parsed._replace(netloc=host)
            value = urlunsplit(sanitized)
    except Exception:
        # 静默忽略：URL 解析失败，保留原始值
        pass

    return value if len(value) <= 120 else value[:120] + "..."

def execute_command(
    command: str,
    cwd: str = None,
    timeout: int = DEFAULT_TIMEOUT,
    capture_output: bool = True,
    check_safety: bool = True,
    shell: bool = False,
    input_text: Optional[str] = None,
    save_output: bool = True,
) -> Tuple[str, str, int, bool, Optional[Dict[str, Any]]]:
    """
    执行非交互式 Shell 命令

    Args:
        command: 要执行的命令
        cwd: 工作目录，默认为当前目录
        timeout: 超时时间（秒）
        capture_output: 是否捕获输出
        check_safety: 是否执行安全检查
        shell: 是否使用平台 shell 解析命令（默认 False 使用 shlex 分词以避免 shell 注入风险）
        input_text: 可选的一次性 stdin 输入；None 表示不连接 stdin
        save_output: 当输出超长时是否保存到文件

    Returns:
        (stdout, stderr, returncode, is_dangerous, output_meta)
        output_meta: 包含 truncated(是否截断), stdout_path, stderr_path 等信息
    """
    is_dangerous = False
    timeout = _coerce_timeout(timeout)

    try:
        stdin_payload = _normalize_input_text(input_text)
    except ValueError as e:
        return ("", f"⚠️ stdin 输入无效: {e}", 1, False, None)
    
    # 安全检查
    if check_safety:
        safety_result = check_command_safety(command)
        if not safety_result['is_safe']:
            return ("", f"⚠️ 安全检查失败: {safety_result['reason']}", 1, True, None)
        is_dangerous = safety_result['is_dangerous']
    
    # 设置工作目录
    if cwd:
        try:
            work_dir = _resolve_work_dir(cwd)
        except ValueError as e:
            return ("", f"⚠️ 工作目录不安全: {e}", 1, False, None)
        if not work_dir.exists():
            return ("", f"❌ 工作目录不存在: {cwd}", 1, False, None)
    else:
        work_dir = _resolve_work_dir()
    
    try:
        cmd_args, run_shell = _build_process_args(command, shell=shell)
        stdout_target = subprocess.PIPE if capture_output else None
        stderr_target = subprocess.PIPE if capture_output else None
        stdin_target = subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL

        process = subprocess.Popen(
            cmd_args,
            cwd=str(work_dir),
            env=build_process_env(),
            stdin=stdin_target,
            stdout=stdout_target,
            stderr=stderr_target,
            text=True,
            errors="replace",
            shell=run_shell,
            **popen_platform_kwargs(),
        )

        try:
            stdout, stderr = process.communicate(input=stdin_payload, timeout=timeout)

            # 保存超长输出到文件
            meta: Dict[str, Any] = {"truncated": False, "stdout_path": None, "stderr_path": None}
            if save_output:
                stdout_truncated = len(stdout or "") > MAX_OUTPUT_LENGTH
                stderr_truncated = len(stderr or "") > MAX_OUTPUT_LENGTH
                if stdout_truncated:
                    out_path = _save_output_to_file(stdout, "", command, "stdout")
                    meta["stdout_path"] = str(out_path) if out_path else None
                    meta["truncated"] = True
                if stderr_truncated:
                    err_path = _save_output_to_file("", stderr, command, "stderr")
                    meta["stderr_path"] = str(err_path) if err_path else None
                    meta["truncated"] = True

            return (
                _build_truncation_summary(stdout, "STDOUT", meta.get("stdout_path")) if save_output and len(stdout or "") > MAX_OUTPUT_LENGTH
                else _truncate_output(stdout, "输出"),
                _build_truncation_summary(stderr, "STDERR", meta.get("stderr_path")) if save_output and len(stderr or "") > MAX_OUTPUT_LENGTH
                else _truncate_output(stderr, "错误输出"),
                int(process.returncode or 0),
                is_dangerous,
                meta if save_output else None,
            )
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    # 静默忽略：进程已终止或权限不足，属非关键路径
                    pass
                stdout, stderr = process.communicate()

            timeout_message = (
                f"⏱️ 命令执行超时（{timeout}秒），已终止进程树。"
                "如果该命令需要交互输入，请使用 input_text 一次性传入输入，"
                "或改为命令行参数、配置文件、环境变量、here-string/管道输入。"
            )
            stderr = (stderr or "").rstrip()
            stderr = f"{stderr}\n{timeout_message}" if stderr else timeout_message

            meta: Dict[str, Any] = {"truncated": False, "stdout_path": None, "stderr_path": None}
            if save_output and stdout and len(stdout) > MAX_OUTPUT_LENGTH:
                out_path = _save_output_to_file(stdout, "", command, "stdout")
                meta["stdout_path"] = str(out_path) if out_path else None
                meta["truncated"] = True

            return (
                _build_truncation_summary(stdout, "STDOUT", meta.get("stdout_path")) if meta["truncated"]
                else _truncate_output(stdout, "输出"),
                _truncate_output(stderr, "错误输出"),
                124,
                is_dangerous,
                meta if save_output else None,
            )
        
    except FileNotFoundError as e:
        return ("", f"❌ 命令不存在: {e}", 127, False, None)
    except Exception as e:
        return ("", f"❌ 执行命令出错: {str(e)}", 1, False, None)


def execute_python(code: str, cwd: str = None) -> Tuple[str, str, int]:
    """
    执行 Python 代码
    
    Args:
        code: Python 代码
        cwd: 工作目录
        
    Returns:
        (stdout, stderr, returncode)
    """
    # 检查是否有危险的代码模式
    danger_patterns = [
        r'import\s+os\s*;.*system',
        r'__import__\s*\(',
        r'exec\s*\(',
        r'eval\s*\(',
        r'subprocess\.run.*shell\s*=\s*True',
        r'open\s*\(.*,\s*[\'"]w[\'"]',
    ]
    
    for pattern in danger_patterns:
        if re.search(pattern, code, re.IGNORECASE):
            return (
                "",
                "⚠️ 检测到潜在危险的代码模式",
                1
            )
    
    # 使用 Python 执行
    cmd = [sys.executable, '-c', code]
    
    if cwd:
        try:
            work_dir = _resolve_work_dir(cwd)
        except ValueError as e:
            return ("", f"⚠️ 工作目录不安全: {e}", 1)
    else:
        work_dir = _resolve_work_dir()
    
    try:
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        
        return (
            result.stdout if result.stdout else "",
            result.stderr if result.stderr else "",
            result.returncode
        )
    except Exception as e:
        return ("", f"❌ 执行 Python 代码出错: {str(e)}", 1)


# ==============================================================================
# LangChain Tools
# ==============================================================================

@tool
def execute_command_tool(
    command: str,
    cwd: str = None,
    timeout: int = 30,
    input_text: Optional[str] = None,
) -> str:
    """
    执行非交互式 Shell 命令。
    
    参数:
        command: 要执行的命令
        cwd: 工作目录（可选）
        timeout: 超时时间，单位秒（默认30秒，最大120秒）
        input_text: 可选的一次性 stdin 输入。不要传入密钥、令牌等敏感信息。
    
    返回:
        命令执行结果（包含 stdout、stderr 和返回码）

    注意:
        该工具不会连接实时交互式 stdin。需要输入的命令应使用 input_text
        一次性传入，或改为命令行参数、环境变量、配置文件、
        here-string/管道输入。
    """
    permission_error = enforce_tool_permission(
        "execute_command_tool",
        {"command": command, "cwd": cwd or "", "timeout": timeout},
    )
    if permission_error:
        return permission_error

    effective_timeout = _coerce_timeout(timeout)
    stdout, stderr, returncode, is_dangerous, output_meta = execute_command(
        command=command,
        cwd=cwd,
        timeout=effective_timeout,
        input_text=input_text,
        check_safety=True,
        shell=True,
        save_output=True,
    )
    
    # 构建输出
    lines = []
    
    if is_dangerous:
        lines.append("⚠️ 【警告】这是一个需要谨慎执行的命令")
        lines.append("")
    
    lines.append(f"📋 命令: {command}")
    lines.append(f"📁 工作目录: {cwd or '当前目录'}")
    lines.append(f"⏱️ 超时时间: {effective_timeout}秒")
    if input_text is not None:
        lines.append(f"⌨️ stdin: 已提供 {len(str(input_text))} 字符")
    else:
        lines.append("⌨️ stdin: 未连接（非交互模式）")
    lines.append("")
    
    if stdout:
        lines.append("📤 标准输出:")
        lines.append(stdout)
        lines.append("")
    
    if stderr:
        lines.append("📥 标准错误:")
        lines.append(stderr)
        lines.append("")
    
    lines.append(f"✅ 返回码: {returncode}")
    
    if returncode == 0:
        lines.append("✅ 命令执行成功")
    else:
        lines.append("❌ 命令执行失败")
    
    return "\n".join(lines)


@tool
def check_command_safety_tool(command: str) -> str:
    """
    检查命令的安全性。
    
    参数:
        command: 要检查的命令
    
    返回:
        安全性检查结果
    """
    result = check_command_safety(command)
    
    lines = []
    lines.append(f"📋 检查命令: {command}")
    lines.append("")
    
    if result['is_dangerous']:
        lines.append("🔴 危险命令")
        lines.append(f"⚠️ 原因: {result['reason']}")
        lines.append(f"⚠️ 危险等级: {result['severity']}")
    elif result['is_safe']:
        lines.append("🟢 安全命令")
        lines.append(f"✅ {result['reason']}")
    else:
        lines.append("🟡 需要注意的命令")
        lines.append(f"ℹ️ {result['reason']}")
    
    return "\n".join(lines)


@tool
def get_system_info() -> str:
    """
    获取系统基本信息。
    
    返回:
        系统信息字符串
    """
    import platform
    
    lines = []
    lines.append("🖥️ 系统信息:")
    lines.append(f"  操作系统: {platform.system()}")
    lines.append(f"  版本: {platform.version()}")
    lines.append(f"  架构: {platform.machine()}")
    lines.append(f"  主机名: {platform.node()}")
    lines.append(f"  Python 版本: {platform.python_version()}")
    
    return "\n".join(lines)


@tool
def list_environment_variables() -> str:
    """
    列出当前环境中的安全诊断变量。
    
    返回:
        环境变量列表
    """

    lines = ["🔧 安全环境变量:"]
    visible_keys = {
        "CI",
        "COLORTERM",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "OS",
        "PATH",
        "PATHEXT",
        "PWD",
        "SHELL",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "VIRTUAL_ENV",
    }
    visible_prefixes = ("SAYA_", "PYTHON", "PIP_")

    shown = 0
    hidden = 0
    for key, value in sorted(os.environ.items()):
        normalized = key.upper()
        if normalized in visible_keys or normalized.startswith(visible_prefixes):
            display_value = _mask_env_value(key, value)
            lines.append(f"  {key} = {display_value}")
            shown += 1
        else:
            hidden += 1

    lines.append("")
    lines.append(f"✅ 已显示 {shown} 个安全变量")
    lines.append(f"🔒 已隐藏 {hidden} 个其他变量")
    lines.append("ℹ️ 默认不会暴露完整环境，避免把凭据和会话信息泄漏给模型。")

    return "\n".join(lines)


# ==============================================================================
# 输出文件读取工具
# ==============================================================================

@tool
def read_output_file(
    path: str,
    tail: Optional[int] = None,
    grep: Optional[str] = None,
    head: Optional[int] = None,
) -> str:
    """
    读取之前保存的命令输出文件，支持按行范围读取和关键词搜索。

    当 shell 命令输出过长时，完整输出会被保存到文件中。
    使用此工具可按需读取文件的特定部分。

    参数:
        path: 文件名（如 "stdout_143022_a1b2c3d4.out"）或相对路径
        tail: 只返回最后 N 行（可选）
        grep: 只返回包含该关键词的行（可选）
        head: 只返回前 N 行（可选）

    返回:
        文件内容或匹配结果

    示例:
        read_output_file(path="stdout_143022_a1b2c3d4.out")
        read_output_file(path="stdout_143022_a1b2c3d4.out", tail=50)
        read_output_file(path="stdout_143022_a1b2c3d4.out", grep="error")
    """
    permission_error = enforce_tool_permission(
        "read_output_file",
        {"path": path, "tail": tail, "grep": grep, "head": head},
    )
    if permission_error:
        return permission_error

    output_dir = _get_output_dir()
    try:
        filepath = _resolve_output_file_path(path)
        normalized_tail = _coerce_line_limit(tail, "tail")
        normalized_head = _coerce_line_limit(head, "head")
    except ValueError as e:
        return f"⚠️ 安全警告: {e}"

    if not filepath.exists():
        available = "\n".join(f"  - {f.name}" for f in sorted(output_dir.iterdir())[:20])
        return f"❌ 文件不存在: {path}\n可用文件:\n{available}"

    if not filepath.is_file():
        return f"❌ 路径不是文件: {path}"

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        return f"❌ 读取文件失败: {e}"

    lines = content.split("\n")
    total_lines = len(lines)

    matched_lines = lines

    if grep:
        matched_lines = [line for line in lines if grep.lower() in line.lower()]
        if not matched_lines:
            return f"🔍 在 {path} 中未找到匹配 '{grep}' 的行（共 {total_lines} 行）"

    if normalized_tail is not None:
        matched_lines = matched_lines[-normalized_tail:] if normalized_tail else []

    if normalized_head is not None:
        matched_lines = matched_lines[:normalized_head]

    result = "\n".join(matched_lines)
    char_count = len(result)
    line_count = len(matched_lines)

    header = f"📄 {path} ({line_count} 行 / {char_count} 字符 / 文件共 {total_lines} 行)"
    if grep:
        header += f" / 匹配: '{grep}'"

    return f"{header}\n{'-' * 40}\n{result}"


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'set_default_workspace',
    'get_default_workspace',
    'execute_command',
    'execute_command_tool',
    'read_output_file',
    'check_command_safety',
    'check_command_safety_tool',
    'sanitize_command',
    'execute_python',
    'get_system_info',
    'list_environment_variables',
]
