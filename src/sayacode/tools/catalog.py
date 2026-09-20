"""维护模型可见的唯一工具清单，不承担调用分发。"""

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
    """返回原生工具；实际并行与审批由 LangGraph 处理。"""
    return list(TOOLS)


def tool_catalog(tools: list[BaseTool] | None = None) -> list[dict[str, Any]]:
    """将真实工具 schema 投影给终端展示。"""
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
