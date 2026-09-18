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


class ContextModifierQueue:
    """并发批次的上下文变更排队，整批完成后才应用。"""
    def __init__(self):
        """初始化空的变更排队队列。"""
        self._pending: list = []

    def enqueue(self, modifier) -> None:
        """排入单个上下文变更，等待整批完成后应用。"""
        self._pending.append(modifier)

    def apply_all(self) -> None:
        """依次应用排队的上下文变更并清空队列。"""
        for modifier in self._pending:
            try:
                modifier()
            except Exception:
                # 忽略单个变更失败，继续应用其余变更。
                pass
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        """返回当前排队的变更数量。"""
        return len(self._pending)



def resolve_tool_workspace(context_or_workspace: Any) -> Path:
    """将运行时上下文、执行上下文或原始路径解析为工作区。"""
    if isinstance(context_or_workspace, ToolExecutionContext):
        return context_or_workspace.workspace

    workspace = getattr(context_or_workspace, "workspace", context_or_workspace)
    return Path(workspace).expanduser().resolve()


@contextmanager
def tool_execution_session(context_or_workspace: Any) -> Iterator[None]:
    """为一次工具调用绑定 file、shell、git、project、permission 与 Hook 服务。"""
    from ..core.hooks import hook_runtime_session, hook_workspace_session
    from ..core.permissions import permission_runtime_session, permission_workspace_session
    from .file_tools import reset_workspace as reset_file_workspace, use_workspace as use_file_workspace
    from .git_tools import reset_workspace as reset_git_workspace, use_workspace as use_git_workspace
    from .project_tools import reset_workspace as reset_project_workspace, use_workspace as use_project_workspace
    from .shell_tools import reset_workspace as reset_shell_workspace, use_workspace as use_shell_workspace

    workspace = resolve_tool_workspace(context_or_workspace)
    permission_runtime = getattr(context_or_workspace, "permissions", None)
    hook_runtime = getattr(context_or_workspace, "hooks", None)

    # 获取本轮中止控制器，绑定到当前上下文。
    abort_ctrl = getattr(context_or_workspace, "_abort_controller", None)
    if abort_ctrl is not None:
        set_abort_controller(abort_ctrl)

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
    "ContextModifierQueue",
    "ToolAbortController",
    "ToolExecutionContext",
    "get_abort_controller",
    "resolve_tool_workspace",
    "set_abort_controller",
    "tool_execution_session",
]
