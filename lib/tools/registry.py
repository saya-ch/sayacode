"""运行时感知的工具工厂接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from typing import Any, List

from langchain_core.tools import BaseTool
from langchain_core.tools import StructuredTool

from ..core.tool_meta import ToolMeta, get_tool_meta, register_tool_meta
from .batch_executor import create_batch_execute_tool
from .context import ToolExecutionContext, tool_execution_session
from .tool_search import create_deferred_tool_invoke_tool, create_tool_search_tool


@dataclass
class ToolRegistry:
    """为一个运行时上下文构建工具列表。"""

    context: Any
    catalog: List[BaseTool] = field(default_factory=list, init=False)

    def build_tools(self) -> List[BaseTool]:
        """返回绑定到上下文工作区的工具。"""
        from . import _get_builtin_tools

        execution_context = ToolExecutionContext.from_runtime(self.context)
        self.catalog = [
            _bind_tool_to_context(tool_obj, execution_context)
            for tool_obj in _get_builtin_tools()
        ]

        return self.compose_tools()

    def compose_tools(
        self,
        additional_tools: List[BaseTool] | None = None,
    ) -> List[BaseTool]:
        """从核心工具与外部工具组合出模型可见的工具集。

        外部工具被延迟加载，并在并发上采用 Fail-Closed。这样可以让庞大的 MCP schema
        不出现在初始模型请求中，同时保留 ``invoke_tool`` 背后原有的 MCP 权限、Hook
        与审计路径。
        """
        from . import _wrap_tool_with_hooks

        execution_context = ToolExecutionContext.from_runtime(self.context)
        all_tools = _deduplicate_tools([*self.catalog, *(additional_tools or [])])
        core_names = {
            str(getattr(tool, "name", ""))
            for tool in self.catalog
        }
        external_names = {
            str(getattr(tool, "name", ""))
            for tool in all_tools
            if str(getattr(tool, "name", "")) not in core_names
        }

        for tool in all_tools:
            name = str(getattr(tool, "name", ""))
            if not name:
                continue
            meta = get_tool_meta(name)
            if meta is None:
                description = str(getattr(tool, "description", "") or "")[:2000]
                register_tool_meta(ToolMeta.safe_default(
                    name,
                    description=description,
                    tool_group="mcp" if name.startswith("mcp_") else "other",
                    search_hint=description[:500],
                    should_defer=name in external_names,
                ))
            elif name in external_names:
                # 外部 schema 不会仅仅因为存在同名但陈旧/全局的元数据，
                # 就变得提前对模型可见。
                meta.should_defer = True
                meta.always_load = False

        # 让可搜索的元数据保持有用，同时避免手工重复每个工具的 description。
        # 元数据注册表仍是编排策略的来源；LangChain 工具仍是 schema 的来源。
        for tool in all_tools:
            meta = get_tool_meta(str(getattr(tool, "name", "")))
            if meta is not None and not meta.description:
                meta.description = str(getattr(tool, "description", "") or "")

        deferred_tools = [
            tool
            for tool in all_tools
            if (
                (meta := get_tool_meta(str(getattr(tool, "name", ""))))
                and meta.should_defer
                and not meta.always_load
            )
        ]
        deferred_names = {
            str(getattr(tool, "name", ""))
            for tool in deferred_tools
        }
        initial_tools = [
            tool
            for tool in all_tools
            if str(getattr(tool, "name", "")) not in deferred_names
        ]

        search_tool = _bind_tool_to_context(
            _wrap_tool_with_hooks(create_tool_search_tool(all_tools)),
            execution_context,
        )
        invoke_tool = _bind_tool_to_context(
            _wrap_tool_with_hooks(create_deferred_tool_invoke_tool(deferred_tools)),
            execution_context,
        )
        batch_tool = _bind_tool_to_context(
            _wrap_tool_with_hooks(create_batch_execute_tool(all_tools)),
            execution_context,
        )
        return [*initial_tools, search_tool, invoke_tool, batch_tool]


def ToolFactory(context: Any) -> List[BaseTool]:
    """用于构造绑定运行时的工具的兼容工厂。"""
    return ToolRegistry(context).build_tools()


__all__ = ["ToolFactory", "ToolRegistry"]


def _deduplicate_tools(tools: List[BaseTool]) -> List[BaseTool]:
    """对每个名称保留第一个工具，使扩展无法遮蔽核心工具。"""
    unique: List[BaseTool] = []
    seen: set[str] = set()
    for tool in tools:
        name = str(getattr(tool, "name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(tool)
    return unique


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
