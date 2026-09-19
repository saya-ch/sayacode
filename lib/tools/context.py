"""运行时作用域的工具执行上下文。

承载工作区绑定与中止信号传递，核心为 ToolExecutionContext 与 ToolAbortController。
调用链为 ToolRegistry 构造执行上下文后经 tool_execution_session 绑定各工具工作区。
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..core.abort import (
    ToolAbortController,
    get_abort_controller,
    set_abort_controller,
)


@dataclass(frozen=True)
class ToolExecutionContext:
    """针对某个运行时工作区执行单个工具所需的状态。"""

    workspace: Path
    permissions: Any = None
    hooks: Any = None
    # 用 Any 承接外部权限与钩子动态对象
    mode: str = "build"

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())

    @classmethod
    def from_runtime(cls, runtime_context: Any) -> "ToolExecutionContext":
        # 用 Any 承接运行时上下文动态结构
        """从运行时上下文构造工具执行上下文。"""
        workspace = Path(getattr(runtime_context, "workspace")).expanduser().resolve()
        permissions = getattr(runtime_context, "permissions", None)
        hooks = getattr(runtime_context, "hooks", None)
        if permissions is not None and hasattr(permissions, "configure_workspace"):
            permissions.configure_workspace(workspace)
        return cls(
            workspace=workspace,
            permissions=permissions,
            hooks=hooks,
            mode=str(getattr(runtime_context, "agent_mode", "build") or "build"),
        )


def resolve_tool_workspace(context_or_workspace: Any) -> Path:
    # 用 Any 承接上下文或路径动态输入
    """将运行时上下文、执行上下文或原始路径解析为工作区。"""
    if isinstance(context_or_workspace, ToolExecutionContext):
        return context_or_workspace.workspace

    workspace = getattr(context_or_workspace, "workspace", context_or_workspace)
    return Path(workspace).expanduser().resolve()


@contextmanager
def tool_execution_session(context_or_workspace: Any) -> Iterator[None]:
    # 用 Any 承接上下文或路径动态输入
    """为一次工具调用绑定 file、shell、git、project、permission 与 Hook 服务。"""
    from .file_tools import reset_workspace as reset_file_workspace, use_workspace as use_file_workspace
    from .git_tools import reset_workspace as reset_git_workspace, use_workspace as use_git_workspace
    from .project_tools import reset_workspace as reset_project_workspace, use_workspace as use_project_workspace
    from .shell_tools import reset_workspace as reset_shell_workspace, use_workspace as use_shell_workspace
    from ..core.hooks import hook_runtime_session, hook_workspace_session
    from ..core.permissions import permission_runtime_session, permission_workspace_session

    workspace = resolve_tool_workspace(context_or_workspace)
    permission_runtime = getattr(context_or_workspace, "permissions", None)
    hook_runtime = getattr(context_or_workspace, "hooks", None)

    # 工作区槽位表：file/shell/git/project 四组绑定函数对（惰性导入防循环）。
    slots = [
        (use_file_workspace, reset_file_workspace),
        (use_shell_workspace, reset_shell_workspace),
        (use_git_workspace, reset_git_workspace),
        (use_project_workspace, reset_project_workspace),
    ]

    tokens: list = []
    try:
        with ExitStack() as stack:
            for use_workspace, _ in slots:
                tokens.append(use_workspace(workspace))
            # 权限/ Hook 会话单点收敛：运行时优先，否则按工作区回落。
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
        for (_, reset_workspace), token in zip(slots, tokens):
            if token is not None:
                reset_workspace(token)


__all__ = [
    "ToolAbortController",
    "ToolExecutionContext",
    "get_abort_controller",
    "resolve_tool_workspace",
    "set_abort_controller",
    "tool_execution_session",
]
