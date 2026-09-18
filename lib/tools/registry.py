"""运行时感知的工具工厂接口。

负责按运行时上下文装配模型可见工具，核心为 ToolRegistry 与 ToolFactory。
调用链为 ToolRegistry.build_tools 绑定工作区后经 compose_tools 完成延迟加载装配。
"""

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
    _execution_context: Any = field(default=None, init=False, repr=False)

    def _get_execution_context(self) -> Any:
        """取本注册表复用的工具执行上下文，避免重复解析工作区。"""
        if self._execution_context is None:
            self._execution_context = ToolExecutionContext.from_runtime(self.context)
        return self._execution_context

    def build_tools(self) -> List[BaseTool]:
        """返回绑定到上下文工作区的工具（含计划/委托装配链）。"""
        from . import _get_builtin_tools

        execution_context = self._get_execution_context()
        self.catalog = [
            _bind_tool_to_context(tool_obj, execution_context)
            for tool_obj in _get_builtin_tools()
        ]
        # 接入计划/委托装配链：失败则返回空，保持核心工具可用。
        for extra in _build_plan_delegate_tools(self.context):
            self.catalog.append(_bind_tool_to_context(extra, execution_context))

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

        execution_context = self._get_execution_context()
        all_tools = _deduplicate_tools([*self.catalog, *(additional_tools or [])])
        external_names = _external_tool_names(self.catalog, all_tools)

        _ensure_tool_metas(all_tools, external_names)

        deferred_tools, initial_tools = _split_deferred(all_tools)

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
        name = _tool_name(tool).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(tool)
    return unique


def _tool_name(tool_obj: BaseTool) -> str:
    """取工具名（单点收敛，避免各处重复拼 getattr）。"""
    return str(getattr(tool_obj, "name", "") or "")


def _external_tool_names(
    core_tools: List[BaseTool],
    all_tools: List[BaseTool],
) -> set[str]:
    """取去重后非核心的外部工具名：同名外部已在去重时丢弃，防遮蔽在此收敛。"""
    core_names = {_tool_name(tool) for tool in core_tools if _tool_name(tool)}
    return {
        _tool_name(tool) for tool in all_tools
        if _tool_name(tool) and _tool_name(tool) not in core_names
    }


def _ensure_tool_metas(all_tools: List[BaseTool], external_names: set[str]) -> None:
    """补齐工具元数据：缺失则注册，外部则强制延迟加载，并回填可搜索描述。"""
    for tool in all_tools:
        name = _tool_name(tool)
        if not name:
            continue
        meta = get_tool_meta(name)
        if meta is None:
            description = str(getattr(tool, "description", "") or "")[:2000]
            meta = register_tool_meta(ToolMeta.safe_default(
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
        if not meta.description:
            meta.description = str(getattr(tool, "description", "") or "")


def _split_deferred(all_tools: List[BaseTool]) -> tuple[List[BaseTool], List[BaseTool]]:
    """按元数据拆分延迟加载组与初始可见组。"""
    deferred_tools = [
        tool
        for tool in all_tools
        if (
            (meta := get_tool_meta(_tool_name(tool)))
            and meta.should_defer
            and not meta.always_load
        )
    ]
    deferred_names = {_tool_name(tool) for tool in deferred_tools}
    initial_tools = [
        tool for tool in all_tools if _tool_name(tool) not in deferred_names
    ]
    return deferred_tools, initial_tools


def _bind_tool_to_context(tool_obj: BaseTool, execution_context: ToolExecutionContext) -> BaseTool:
    # 去重：同一工作区重复绑定直接复用，避免会话嵌套加深。
    bound_workspace = getattr(tool_obj, "_sayacode_bound_workspace", None)
    if bound_workspace is not None and str(bound_workspace) == str(execution_context.workspace):
        return tool_obj
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
    bound = StructuredTool.from_function(
        func=bound_func,
        name=tool_name,
        description=description,
        args_schema=args_schema,
        infer_schema=args_schema is None,
        return_direct=return_direct,
        response_format=response_format,
    )
    # 保留 Hook 标记：original_func 已内含 Hook 包裹，装配不叠加第二层。
    if getattr(tool_obj, "_sayacode_hooks_wrapped", False):
        setattr(bound, "_sayacode_hooks_wrapped", True)
    setattr(bound, "_sayacode_bound_workspace", str(execution_context.workspace))
    return bound


def _try_extend_plan_delegate(extras: List[BaseTool], builder: Any) -> None:
    """执行一个计划/委托拼装分支，失败吞掉以保持核心工具可用。"""
    try:
        extras.extend(builder())
    except Exception:
        pass


def _build_plan_delegate_tools(context: Any) -> List[BaseTool]:
    """构建计划（3）+委托（2：同步委托与同步追问）工具，失败返回空列表。"""
    extras: List[BaseTool] = []

    def _build_plan_tools() -> List[BaseTool]:
        from ..core.plans import PlanStore
        from .plan_tools import create_plan_tools

        return list(create_plan_tools(lambda: PlanStore.for_runtime(context)))

    def _build_delegate_tools() -> List[BaseTool]:
        from pathlib import Path as _Path

        from ..core.team_supervisor import TeamSupervisor
        from .delegate_tools import (
            build_manager_resume_fn,
            build_manager_spawn_fn,
            create_delegate_tool,
            create_sync_resume_tool,
        )

        workspace = _Path(getattr(context, "workspace", _Path.cwd())).resolve()
        try:
            from ..core.paths import SayacodePaths

            home = _Path(str(SayacodePaths.resolve().home)).resolve()
        except Exception:
            home = _Path.home().resolve()
        try:
            delegate_tools = list(getattr(context, "tools", []) or [])
        except Exception:
            delegate_tools = []
        supervisor = TeamSupervisor(
            model=getattr(context, "model", None),
            workspace=workspace,
            runtime=context,
            tools=delegate_tools,
            home=home,
        )
        spawn = build_manager_spawn_fn(supervisor, workspace)
        resume = build_manager_resume_fn(supervisor)
        return [create_delegate_tool(spawn), create_sync_resume_tool(resume)]

    # 两分支共用同一容错收敛点，此前为两段重复的 try/except。
    _try_extend_plan_delegate(extras, _build_plan_tools)
    _try_extend_plan_delegate(extras, _build_delegate_tools)
    return extras
