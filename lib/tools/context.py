"""运行时作用域的工具执行上下文。

承载工作区绑定与中止信号传递，核心为 ToolExecutionContext 与 ToolAbortController。
调用链为 ToolRegistry 构造执行上下文后经 tool_execution_session 绑定各工具工作区。
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class ToolExecutionContext:
    """针对某个运行时工作区执行单个工具所需的状态。"""

    workspace: Path
    permissions: Any = None
    hooks: Any = None
    mode: str = "build"

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())

    @classmethod
    def from_runtime(cls, runtime_context: Any) -> "ToolExecutionContext":
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


# 管理工具级中止信号，仅作用于同级调用。


@dataclass
class ToolAbortController:
    """工具级中止控制器 — 参考 Claude Code siblingAbortController.

    Bash/Shell/Git 类工具执行失败时，向同级工具发送 abort 信号。
    只杀死同级（sibling），不传播到父级（parent），也不结束整轮：
    整轮是否结束由 ``ToolCallLimitMiddleware(exit_behavior="end")`` 按调用
    配额决定，两者正交——本控制器只影响同批次剩余调用（见 batch_executor
    的 sibling-abort），配额中间件只管“调用次数到顶就收尾”，互不替代。
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


# 用 ContextVar 传递中止控制器，每轮重置。
_ABORT_CONTROLLER: ContextVar[ToolAbortController] = ContextVar(
    "_sayacode_abort_controller", default=ToolAbortController()
)


def get_abort_controller() -> ToolAbortController:
    """获取当前上下文的工具中止控制器。"""
    return _ABORT_CONTROLLER.get()


def set_abort_controller(ctrl: ToolAbortController) -> None:
    """设置当前上下文的工具中止控制器。"""
    _ABORT_CONTROLLER.set(ctrl)


def resolve_tool_workspace(context_or_workspace: Any) -> Path:
    """将运行时上下文、执行上下文或原始路径解析为工作区。"""
    if isinstance(context_or_workspace, ToolExecutionContext):
        return context_or_workspace.workspace

    workspace = getattr(context_or_workspace, "workspace", context_or_workspace)
    return Path(workspace).expanduser().resolve()


@contextmanager
def tool_execution_session(context_or_workspace: Any) -> Iterator[None]:
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
