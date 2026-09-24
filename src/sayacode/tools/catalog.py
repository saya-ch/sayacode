"""维护模型可见的唯一工具清单，不承担调用分发。

清单是单例表，构造只拷贝不新建，并发调用共用同一套定义。
展示用投影只拿名字描述和参数表，不暴露实现。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from .analysis import analyze_project, list_symbols
from .files import (
    delete_file,
    list_directory,
    read_file,
    read_output_file,
    search_replace,
    write_file,
)
from .git import git
from .shell import execute_command_tool
from .web import web_search

TOOLS = [
    read_file,
    write_file,
    search_replace,
    delete_file,
    list_directory,
    execute_command_tool,
    read_output_file,
    git,
    analyze_project,
    list_symbols,
    web_search,
]


def build_tools(_context: Any = None) -> list[BaseTool]:
    """做什么，返回原生工具，实际并行与审批由上层处理。

    参数与返回，入参上下文暂不使用，返回清单拷贝。
    调用约束，每次返回新列表，改返回不会污染全局表。
    坑点是这里不做过滤，权限要在调用前由中间件卡。"""
    return list(TOOLS)


def tool_catalog(tools: list[BaseTool] | None = None) -> list[dict[str, Any]]:
    """做什么，将真实工具参数投影给产品界面展示。

    参数与返回，入参可选工具表，默认用全局表，返回名字描述和参数表。
    调用约束，只读参数模型，不触发工具执行。
    坑点是参数模型新老形态不一，有模型的转模型，无模型的原样返回。"""
    selected = TOOLS if tools is None else tools
    return [
        {
            "name": item.name,
            "description": item.description,
            "schema": item.tool_call_schema.model_json_schema()
            if hasattr(item.tool_call_schema, "model_json_schema")
            else item.tool_call_schema,
        }
        for item in selected
    ]
