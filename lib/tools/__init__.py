"""
工具模块

提供当前运行时实际使用的工具集，供 Agent 使用：

文件操作工具 (file_tools):
- read_file: 读取文件内容
- write_file: 写入文件内容
- search_replace: 搜索并替换文件内容
- glob_search: 使用 glob 模式搜索文件
- grep_search: 在文件中搜索内容
- create_directory: 创建目录
- delete_file: 删除文件
- list_directory: 列出目录内容

Shell 工具 (shell_tools):
- execute_command: 执行 Shell 命令
- check_command_safety: 检查命令安全性

Git 工具 (git_tools):
- git_status: 查看工作区状态
- git_diff: 查看文件修改
- git_commit: 提交更改
- git_log: 查看提交历史
- git_branch: 查看分支
- git_checkout: 切换分支
- git_add: 暂存文件
- git_stash: 暂存工作区修改
- git_pull: 拉取远程更新
- git_push: 推送到远程仓库

项目分析工具 (project_tools):
- analyze_project: 分析项目结构
- get_project_summary: 获取项目摘要
- list_project_files: 列出项目文件
- get_file_info: 获取文件信息
- list_symbols: 列出项目代码符号
- find_symbol: 定位项目代码符号

安全工具 (safety):
- check_file_danger: 检查文件操作
- check_command_danger: 检查命令
- check_batch_operation: 检查批量操作
"""

from contextlib import contextmanager
from functools import wraps
from typing import Any, Dict, List

from langchain_core.tools import StructuredTool

from ..core.hooks import (
    configure_hooks_workspace,
    trigger_hook_event,
)
from ..core.audit import append_audit_event
from ..core.permissions import (
    configure_permission_workspace,
)
from .context import ToolExecutionContext, tool_execution_session
from .registry import ToolFactory, ToolRegistry

# 导入所有工具模块
from .file_tools import (
    get_default_workspace as get_file_tools_workspace,
    reset_workspace as reset_file_tools_workspace,
    set_default_workspace as set_file_tools_workspace,
    use_workspace as use_file_tools_workspace,
    read_file,
    write_file,
    search_replace,
    glob_search,
    grep_search,
    create_directory,
    delete_file,
    list_directory,
    batch_edit,
)

from .shell_tools import (
    get_default_workspace as get_shell_tools_workspace,
    reset_workspace as reset_shell_tools_workspace,
    set_default_workspace as set_shell_tools_workspace,
    use_workspace as use_shell_tools_workspace,
    execute_command_tool,
    check_command_safety_tool,
    get_system_info,
    list_environment_variables,
    execute_command,
    read_output_file,
)

from .git_tools import (
    get_default_workspace as get_git_tools_workspace,
    reset_workspace as reset_git_tools_workspace,
    set_default_workspace as set_git_tools_workspace,
    use_workspace as use_git_tools_workspace,
    git_status,
    git_diff,
    git_commit,
    git_log,
    git_branch,
    git_checkout,
    git_add,
    git_stash,
    git_pull,
    git_push,
    git_remote,
)

from .project_tools import (
    get_default_workspace as get_project_tools_workspace,
    reset_workspace as reset_project_tools_workspace,
    set_default_workspace as set_project_tools_workspace,
    use_workspace as use_project_tools_workspace,
    analyze_project,
    get_project_summary,
    list_project_files,
    get_file_info,
    list_symbols,
    find_symbol,
)

from .safety import (
    check_file_danger,
    check_command_danger,
    check_batch_operation,
    SafetyResult,
)


def configure_tool_workspace(workspace: str) -> None:
    """将文件、Shell、Git、项目分析工具的默认工作区统一设置为当前会话工作区。"""
    set_file_tools_workspace(workspace)
    set_shell_tools_workspace(workspace)
    set_git_tools_workspace(workspace)
    set_project_tools_workspace(workspace)
    configure_permission_workspace(workspace)
    configure_hooks_workspace(workspace)


@contextmanager
def tool_workspace_session(context_or_workspace: str | ToolExecutionContext):
    """在指定工作区内临时执行工具调用，结束后恢复之前的配置。"""
    with tool_execution_session(context_or_workspace):
        yield


_TOOL_GROUP_LABELS = {
    "lib.tools.file_tools": "File Operations",
    "lib.tools.shell_tools": "Shell Commands",
    "lib.tools.git_tools": "Git Operations",
    "lib.tools.project_tools": "Project Analysis",
}


def _wrap_tool_with_hooks(tool_obj: Any) -> Any:
    """Wrap a LangChain StructuredTool so every invocation emits hook events."""
    if getattr(tool_obj, "_sayacode_hooks_wrapped", False):
        return tool_obj

    original_func = getattr(tool_obj, "func", None)
    if original_func is None:
        return tool_obj

    tool_name = str(getattr(tool_obj, "name", original_func.__name__))

    @wraps(original_func)
    def wrapped_func(*args: Any, **kwargs: Any) -> Any:
        arguments = _coerce_tool_arguments(tool_obj, args, kwargs)
        block_reason = trigger_hook_event(
            "PreToolUse",
            {"tool_name": tool_name, "arguments": arguments},
        )
        if block_reason:
            return f"⚠️ {block_reason}"

        try:
            result = original_func(*args, **kwargs)
        except Exception as exc:
            trigger_hook_event(
                "ToolFailure",
                {
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "error": str(exc),
                    "exception_type": exc.__class__.__name__,
                },
            )
            append_audit_event(
                "tool",
                tool_name,
                workspace=(
                    get_file_tools_workspace()
                    or get_shell_tools_workspace()
                    or get_git_tools_workspace()
                    or get_project_tools_workspace()
                ),
                allowed=False,
                details={
                    "arguments": arguments,
                    "error": str(exc),
                    "exception_type": exc.__class__.__name__,
                },
            )
            raise

        trigger_hook_event(
            "PostToolUse",
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "result_preview": str(result)[:1000],
            },
        )
        append_audit_event(
            "tool",
            tool_name,
            workspace=get_file_tools_workspace() or get_shell_tools_workspace() or get_git_tools_workspace() or get_project_tools_workspace(),
            allowed=True,
            details={"arguments": arguments, "result_preview": str(result)[:1000]},
        )
        return result

    try:
        wrapped_tool = StructuredTool.from_function(
            func=wrapped_func,
            name=tool_name,
            description=getattr(tool_obj, "description", "") or "",
            args_schema=getattr(tool_obj, "args_schema", None),
            return_direct=bool(getattr(tool_obj, "return_direct", False)),
        )
        setattr(wrapped_tool, "_sayacode_hooks_wrapped", True)
        return wrapped_tool
    except Exception:
        setattr(tool_obj, "_sayacode_hooks_wrapped", True)
        return tool_obj


def _coerce_tool_arguments(tool_obj: Any, args: tuple[Any, ...], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    if kwargs:
        return dict(kwargs)

    schema = getattr(tool_obj, "args_schema", None)
    field_names = []
    if schema is not None:
        field_names = list(getattr(schema, "model_fields", {}) or getattr(schema, "__fields__", {}) or {})

    if field_names:
        return {
            field_name: args[index]
            for index, field_name in enumerate(field_names)
            if index < len(args)
        }

    return {f"arg{index}": value for index, value in enumerate(args)}


def _get_builtin_tools() -> List[Any]:
    """Return the built-in tool catalog used by runtime registries."""
    return list(_BUILTIN_TOOLS)


def get_runtime_tool_catalog() -> Dict[str, List[dict]]:
    """根据当前真实注册工具集合生成运行时工具目录。"""
    catalog: Dict[str, List[dict]] = {}

    for tool in _BUILTIN_TOOLS:
        source_module = getattr(getattr(tool, "func", None), "__module__", "") or getattr(tool, "__module__", "")
        group = _TOOL_GROUP_LABELS.get(source_module, "Other")
        description = getattr(tool, "description", "") or ""
        summary = description.strip().splitlines()[0] if description.strip() else ""

        catalog.setdefault(group, []).append(
            {
                "name": getattr(tool, "name", tool.__class__.__name__),
                "summary": summary,
                "module": source_module,
            }
        )

    for items in catalog.values():
        items.sort(key=lambda item: item["name"])

    return dict(sorted(catalog.items(), key=lambda pair: pair[0]))


_RAW_BUILTIN_TOOLS = [
    # 文件操作
    read_file,
    write_file,
    search_replace,
    glob_search,
    grep_search,
    create_directory,
    delete_file,
    list_directory,
    batch_edit,
    
    # Shell 命令
    execute_command_tool,
    check_command_safety_tool,
    get_system_info,
    list_environment_variables,
    read_output_file,
    
    # Git 操作
    git_status,
    git_diff,
    git_log,
    git_branch,
    git_checkout,
    git_add,
    git_commit,
    git_stash,
    git_pull,
    git_push,
    git_remote,
    
    # 项目分析
    analyze_project,
    get_project_summary,
    list_project_files,
    get_file_info,
    list_symbols,
    find_symbol,
]

_BUILTIN_TOOLS = [_wrap_tool_with_hooks(tool_obj) for tool_obj in _RAW_BUILTIN_TOOLS]


def _wrapped_tool(name: str) -> Any:
    for tool_obj in _BUILTIN_TOOLS:
        if str(getattr(tool_obj, "name", "")) == name:
            return tool_obj
    raise RuntimeError(f"Built-in tool is not registered: {name}")


read_file = _wrapped_tool("read_file")
write_file = _wrapped_tool("write_file")
search_replace = _wrapped_tool("search_replace")
glob_search = _wrapped_tool("glob_search")
grep_search = _wrapped_tool("grep_search")
create_directory = _wrapped_tool("create_directory")
delete_file = _wrapped_tool("delete_file")
list_directory = _wrapped_tool("list_directory")
batch_edit = _wrapped_tool("batch_edit")
execute_command_tool = _wrapped_tool("execute_command_tool")
check_command_safety_tool = _wrapped_tool("check_command_safety_tool")
get_system_info = _wrapped_tool("get_system_info")
list_environment_variables = _wrapped_tool("list_environment_variables")
read_output_file = _wrapped_tool("read_output_file")
git_status = _wrapped_tool("git_status")
git_diff = _wrapped_tool("git_diff")
git_log = _wrapped_tool("git_log")
git_branch = _wrapped_tool("git_branch")
git_checkout = _wrapped_tool("git_checkout")
git_add = _wrapped_tool("git_add")
git_commit = _wrapped_tool("git_commit")
git_stash = _wrapped_tool("git_stash")
git_pull = _wrapped_tool("git_pull")
git_push = _wrapped_tool("git_push")
git_remote = _wrapped_tool("git_remote")
analyze_project = _wrapped_tool("analyze_project")
get_project_summary = _wrapped_tool("get_project_summary")
list_project_files = _wrapped_tool("list_project_files")
get_file_info = _wrapped_tool("get_file_info")
list_symbols = _wrapped_tool("list_symbols")
find_symbol = _wrapped_tool("find_symbol")


# 导出列表
__all__ = [
    # 文件操作
    "read_file",
    "write_file",
    "search_replace",
    "glob_search",
    "grep_search",
    "create_directory",
    "delete_file",
    "list_directory",
    "batch_edit",
    "get_file_tools_workspace",
    "set_file_tools_workspace",
    "reset_file_tools_workspace",
    "use_file_tools_workspace",

    # Shell 命令
    "execute_command_tool",
    "check_command_safety_tool",
    "get_system_info",
    "list_environment_variables",
    "execute_command",
    "read_output_file",
    "get_shell_tools_workspace",
    "set_shell_tools_workspace",
    "reset_shell_tools_workspace",
    "use_shell_tools_workspace",

    # Git 操作
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_checkout",
    "git_add",
    "git_commit",
    "git_stash",
    "git_pull",
    "git_push",
    "git_remote",
    "get_git_tools_workspace",
    "set_git_tools_workspace",
    "reset_git_tools_workspace",
    "use_git_tools_workspace",

    # 项目分析
    "analyze_project",
    "get_project_summary",
    "list_project_files",
    "get_file_info",
    "list_symbols",
    "find_symbol",
    "get_project_tools_workspace",
    "set_project_tools_workspace",
    "reset_project_tools_workspace",
    "use_project_tools_workspace",

    # 安全检查
    "check_file_danger",
    "check_command_danger",
    "check_batch_operation",
    "SafetyResult",

    "ToolFactory",
    "ToolRegistry",
    "ToolExecutionContext",
    "get_runtime_tool_catalog",
    "configure_tool_workspace",
    "tool_workspace_session",
]
