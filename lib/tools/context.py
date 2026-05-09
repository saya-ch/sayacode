"""Runtime-scoped tool execution context."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class ToolExecutionContext:
    """State required to execute one tool against one runtime workspace."""

    workspace: Path
    permissions: Any = None
    hooks: Any = None
    mode: str = "build"

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())

    @classmethod
    def from_runtime(cls, runtime_context: Any) -> "ToolExecutionContext":
        return cls(
            workspace=Path(getattr(runtime_context, "workspace")),
            permissions=getattr(runtime_context, "permissions", None),
            hooks=getattr(runtime_context, "hooks", None),
            mode=str(getattr(runtime_context, "agent_mode", "build") or "build"),
        )


# ==============================================================================
# 工具中止控制器
# ==============================================================================


@dataclass
class ToolAbortController:
    """工具级中止控制器 — 参考 Claude Code siblingAbortController.

    Bash/Shell/Git 类工具执行失败时，向同级工具发送 abort 信号。
    只杀死同级（sibling），不传播到父级（parent）。

    用法:
        abort_ctrl = ToolAbortController()
        # 在某个工具失败时:
        abort_ctrl.abort("sibling_error")
        # 其他工具在执行前检查:
        if abort_ctrl.is_aborted:
            return "⚠️ 操作已中止: " + abort_ctrl.reason
    """
    _aborted: bool = False
    _reason: str = ""

    def abort(self, reason: str) -> None:
        """设置中止信号。由失败的工具调用。"""
        self._aborted = True
        self._reason = reason

    @property
    def is_aborted(self) -> bool:
        """检查是否已设置中止信号。"""
        return self._aborted

    @property
    def reason(self) -> str:
        """获取中止原因。"""
        return self._reason or "unknown"

    def reset(self) -> None:
        """重置中止状态（每批工具执行前调用）。"""
        self._aborted = False
        self._reason = ""


# 用于 ContextVar 传递的默认实例
_DEFAULT_ABORT_CONTROLLER = ToolAbortController()


def get_abort_controller() -> ToolAbortController:
    """获取当前上下文的工具中止控制器。"""
    return _DEFAULT_ABORT_CONTROLLER



def resolve_tool_workspace(context_or_workspace: Any) -> Path:
    """Resolve a runtime context, execution context, or raw path to a workspace."""
    if isinstance(context_or_workspace, ToolExecutionContext):
        return context_or_workspace.workspace

    workspace = getattr(context_or_workspace, "workspace", context_or_workspace)
    return Path(workspace).expanduser().resolve()


@contextmanager
def tool_execution_session(context_or_workspace: Any) -> Iterator[None]:
    """Bind file, shell, git, project, permission, and hook services for a tool call."""
    from ..core.hooks import hook_runtime_session, hook_workspace_session
    from ..core.permissions import permission_runtime_session, permission_workspace_session
    from .file_tools import reset_workspace as reset_file_workspace, use_workspace as use_file_workspace
    from .git_tools import reset_workspace as reset_git_workspace, use_workspace as use_git_workspace
    from .project_tools import reset_workspace as reset_project_workspace, use_workspace as use_project_workspace
    from .shell_tools import reset_workspace as reset_shell_workspace, use_workspace as use_shell_workspace

    workspace = resolve_tool_workspace(context_or_workspace)
    permission_runtime = getattr(context_or_workspace, "permissions", None)
    hook_runtime = getattr(context_or_workspace, "hooks", None)
    file_token = shell_token = git_token = project_token = None
    try:
        with ExitStack() as stack:
            file_token = use_file_workspace(workspace)
            shell_token = use_shell_workspace(workspace)
            git_token = use_git_workspace(workspace)
            project_token = use_project_workspace(workspace)
            if permission_runtime is not None:
                stack.enter_context(permission_runtime_session(permission_runtime))
            else:
                stack.enter_context(permission_workspace_session(workspace))
            if hook_runtime is not None:
                stack.enter_context(hook_runtime_session(hook_runtime))
            else:
                stack.enter_context(hook_workspace_session(workspace))
            yield
    finally:
        if file_token is not None:
            reset_file_workspace(file_token)
        if shell_token is not None:
            reset_shell_workspace(shell_token)
        if git_token is not None:
            reset_git_workspace(git_token)
        if project_token is not None:
            reset_project_workspace(project_token)


__all__ = [
    "ToolAbortController",
    "ToolExecutionContext",
    "get_abort_controller",
    "resolve_tool_workspace",
    "tool_execution_session",
]
