"""Runtime-aware tool factory interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, List

from langchain_core.tools import BaseTool
from langchain_core.tools import StructuredTool

from .context import ToolExecutionContext, tool_execution_session


@dataclass
class ToolRegistry:
    """Build tool lists for one runtime context."""

    context: Any

    def build_tools(self) -> List[BaseTool]:
        """Return tools bound to the context workspace."""
        from . import _get_builtin_tools

        execution_context = ToolExecutionContext.from_runtime(self.context)
        return [_bind_tool_to_context(tool_obj, execution_context) for tool_obj in _get_builtin_tools()]


def ToolFactory(context: Any) -> List[BaseTool]:
    """Compatibility factory for constructing runtime-bound tools."""
    return ToolRegistry(context).build_tools()


__all__ = ["ToolFactory", "ToolRegistry"]


def _bind_tool_to_context(tool_obj: BaseTool, execution_context: ToolExecutionContext) -> BaseTool:
    original_func = getattr(tool_obj, "func", None)
    if original_func is None:
        return tool_obj

    tool_name = str(getattr(tool_obj, "name", getattr(original_func, "__name__", "tool")))
    description = str(getattr(tool_obj, "description", "") or "")
    args_schema = getattr(tool_obj, "args_schema", None)
    return_direct = bool(getattr(tool_obj, "return_direct", False))
    response_format = getattr(tool_obj, "response_format", "content")

    @wraps(original_func)
    def bound_func(*args: Any, **kwargs: Any) -> Any:
        with tool_execution_session(execution_context):
            return original_func(*args, **kwargs)

    bound_func.__name__ = tool_name
    return StructuredTool.from_function(
        func=bound_func,
        name=tool_name,
        description=description,
        args_schema=args_schema,
        infer_schema=args_schema is None,
        return_direct=return_direct,
        response_format=response_format,
    )
