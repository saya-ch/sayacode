"""
ToolSearch 工具 — 参考 Claude Code ToolSearchTool.

当工具数量较多（>30 或含 MCP 工具）时，部分工具标记为 should_defer=True，
不会在初始 prompt 中发送完整 schema。模型需要先调用 ToolSearch 按关键字搜索，
然后获得工具的完整定义。

搜索维度：
1. 工具名（精确/前缀/子串匹配）
2. search_hint（关键字提示）
3. tool_group 分组名
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, List, Optional

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..core.tool_meta import get_all_tool_metas


TOOL_SEARCH_MAX_RESULTS = 20
TOOL_SEARCH_MAX_SCHEMA_CHARS = 6000


class ToolSearchInput(BaseModel):
    """ToolSearch 工具输入参数。"""
    query: str = Field(
        description="搜索关键词。在工具名称、search_hint 和分组名中搜索。"
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=TOOL_SEARCH_MAX_RESULTS,
        description="返回的最大工具数。",
    )


@dataclass
class ToolSearchResult:
    """单个搜索结果。"""
    name: str
    group: str
    match_reason: str
    description: str
    search_hint: str
    is_deferred: bool


def _search_tools(
    query: str,
    limit: int = 10,
    available_names: Optional[set[str]] = None,
) -> List[ToolSearchResult]:
    """在已注册的工具元数据中搜索匹配项。"""
    all_metas = get_all_tool_metas()
    query_lower = query.lower().strip()
    results: List[tuple[int, ToolSearchResult]] = []

    for meta in all_metas:
        if available_names is not None and meta.name not in available_names:
            continue
        score = 0
        reason = ""

        name_lower = meta.name.lower()

        # 优先命中精确名称，给予最高分数。
        if query_lower == name_lower:
            score = 100
            reason = "精确名称匹配"
        # 匹配名称前缀，按前缀优先级打分。
        elif name_lower.startswith(query_lower):
            score = 80
            reason = "名称前缀匹配"
        # 匹配名称子串，按包含关系打分。
        elif query_lower in name_lower:
            score = 60
            reason = "名称包含"
        # 匹配 search_hint 关键词，按提示相关性打分。
        elif meta.search_hint and query_lower in meta.search_hint.lower():
            score = 40
            reason = f"关键字匹配: {meta.search_hint}"
        # 匹配分组名，按分组相关性打分。
        elif query_lower in meta.tool_group.lower():
            score = 20
            reason = f"分组: {meta.tool_group}"
        else:
            # 拆词部分匹配，按命中词数累加打分。
            query_words = query_lower.split()
            hint_lower = (meta.search_hint or "").lower()
            word_matches = sum(
                1 for w in query_words
                if w in name_lower or w in hint_lower
            )
            if word_matches > 0:
                score = 10 + word_matches * 5
                reason = f"部分匹配 ({word_matches} 个词)"

        if score > 0:
            results.append((score, ToolSearchResult(
                name=meta.name,
                group=meta.tool_group,
                match_reason=reason,
                description=meta.description,
                search_hint=meta.search_hint,
                is_deferred=meta.should_defer,
            )))

    # 排序并截断结果，依分数降序取前若干项。
    results.sort(key=lambda x: x[0], reverse=True)
    bounded_limit = max(1, min(int(limit), TOOL_SEARCH_MAX_RESULTS))
    return [r for _, r in results[:bounded_limit]]


def _format_search_results(
    results: List[ToolSearchResult],
    tool_details: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """格式化搜索结果为用户可读文本。"""
    if not results:
        return "未找到匹配的工具。请尝试其他关键字。"

    lines = [f"找到 {len(results)} 个匹配工具:"]
    for r in results:
        flags = []
        if r.is_deferred:
            flags.append("延迟加载")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"  • {r.name} ({r.group}){flag_str}\n"
            f"    匹配: {r.match_reason}"
        )
        detail = (tool_details or {}).get(r.name, {})
        description = detail.get("description") or r.description
        if description:
            cleaned_description = str(description).strip()
            if cleaned_description:
                first_line = cleaned_description.splitlines()[0][:500]
                lines.append(f"    说明: {first_line}")
        if r.is_deferred and detail.get("input_schema"):
            schema = json.dumps(detail["input_schema"], ensure_ascii=False, separators=(",", ":"))
            if len(schema) > TOOL_SEARCH_MAX_SCHEMA_CHARS:
                schema = schema[:TOOL_SEARCH_MAX_SCHEMA_CHARS] + "...[schema truncated]"
            lines.append(f"    参数 schema: {schema}")
            lines.append(f"    调用方式: invoke_tool(tool_name=\"{r.name}\", arguments={{...}})")
    return "\n".join(lines)


def tool_search_func(query: str, limit: int = 10) -> str:
    """搜索可用工具并返回匹配结果。

    当模型不确定使用哪个工具时，调用此函数按关键字搜索。
    匹配的工具将获得完整 schema 并可正常调用。
    """
    results = _search_tools(query, limit=limit)
    return _format_search_results(results)


def _extract_input_schema(tool: BaseTool) -> Dict[str, Any]:
    """取工具输入 schema，失败返回空（Fail-Closed：无 schema 就不展示参数）。"""
    try:
        return tool.get_input_schema().model_json_schema()
    except Exception:
        return {}


def _tool_details(tools: Optional[List[BaseTool]]) -> Dict[str, Dict[str, Any]]:
    details: Dict[str, Dict[str, Any]] = {}
    for tool in tools or []:
        name = str(getattr(tool, "name", ""))
        if not name:
            continue
        details[name] = {
            "description": str(getattr(tool, "description", "") or ""),
            "input_schema": _extract_input_schema(tool),
        }
    return details


def create_tool_search_tool(tools: Optional[List[BaseTool]] = None) -> StructuredTool:
    """创建 ToolSearch LangChain 工具实例。"""
    details = _tool_details(tools)
    available_names = set(details) if tools is not None else None

    def runtime_tool_search(query: str, limit: int = 10) -> str:
        return _format_search_results(
            _search_tools(query, limit=limit, available_names=available_names),
            tool_details=details,
        )

    return StructuredTool.from_function(
        func=runtime_tool_search,
        name="ToolSearch",
        description=(
            "搜索可用工具。当你不确定使用哪个工具来完成任务时，"
            "先用关键字搜索匹配的工具名。返回最佳匹配的工具列表。"
            "对于标记为 '延迟加载' 的工具，搜索后可使用其完整定义。"
        ),
        args_schema=ToolSearchInput,
    )


class DeferredToolInvokeInput(BaseModel):
    """调用一个通过 ToolSearch 发现完整 schema 的工具。"""

    tool_name: str = Field(description="ToolSearch 返回的延迟工具名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="符合返回 schema 的参数对象")


def create_deferred_tool_invoke_tool(tools: List[BaseTool]) -> StructuredTool:
    """为延迟加载的工具创建通用的保留策略分发器。

    与图节点直接调用同源，最终都走工具调用，权限与审计由外层包装保障，
    本分发器只做延迟加载网关，不复制那一层逻辑。
    延迟语义靠创建时快照保留，建好后新增工具不会自动可见，必须重建分发器。
    """
    tool_map = {
        str(getattr(tool, "name", "")): tool
        for tool in tools
        if str(getattr(tool, "name", ""))
    }

    def invoke_tool(tool_name: str, arguments: Dict[str, Any]) -> Any:
        # 延迟网关唯一查表点：未知名 Fail-Closed 并列出快照内可用名。
        tool = tool_map.get(tool_name)
        if tool is None:
            available = ", ".join(sorted(tool_map)) or "(none)"
            raise ValueError(f"Unknown deferred tool: {tool_name}. Available: {available}")
        return tool.invoke(dict(arguments or {}))

    return StructuredTool.from_function(
        func=invoke_tool,
        name="invoke_tool",
        description=(
            "调用 ToolSearch 标记为延迟加载的工具。先使用 ToolSearch 获取工具名称和参数 schema，"
            "再把 tool_name 与 arguments 传入；底层权限、Hook 和审计仍然生效。"
        ),
        args_schema=DeferredToolInvokeInput,
    )


__all__ = [
    "ToolSearchInput",
    "ToolSearchResult",
    "create_tool_search_tool",
    "DeferredToolInvokeInput",
    "create_deferred_tool_invoke_tool",
    "tool_search_func",
    "_search_tools",
    "TOOL_SEARCH_MAX_RESULTS",
    "TOOL_SEARCH_MAX_SCHEMA_CHARS",
]
