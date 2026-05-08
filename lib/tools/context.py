"""Runtime-scoped tool execution context."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
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


__all__ = ["ToolExecutionContext", "resolve_tool_workspace", "tool_execution_session"]
