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
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..core.tool_meta import get_all_tool_metas, get_tool_meta


class ToolSearchInput(BaseModel):
    """ToolSearch 工具输入参数。"""
    query: str = Field(
        description="搜索关键词。在工具名称、search_hint 和分组名中搜索。"
    )
    limit: int = Field(
        default=10,
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


def _search_tools(query: str, limit: int = 10) -> List[ToolSearchResult]:
    """在已注册的工具元数据中搜索匹配项。"""
    all_metas = get_all_tool_metas()
    query_lower = query.lower().strip()
    results: List[tuple[int, ToolSearchResult]] = []

    for meta in all_metas:
        score = 0
        reason = ""

        name_lower = meta.name.lower()

        # 精确名称匹配 → 最高分
        if query_lower == name_lower:
            score = 100
            reason = "精确名称匹配"
        # 前缀匹配
        elif name_lower.startswith(query_lower):
            score = 80
            reason = "名称前缀匹配"
        # 名称子串匹配
        elif query_lower in name_lower:
            score = 60
            reason = "名称包含"
        # search_hint 匹配
        elif meta.search_hint and query_lower in meta.search_hint.lower():
            score = 40
            reason = f"关键字匹配: {meta.search_hint}"
        # 分组名匹配
        elif query_lower in meta.tool_group.lower():
            score = 20
            reason = f"分组: {meta.tool_group}"
        else:
            # 分词匹配（查询中的每个词）
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

    # 按分数降序，截断
    results.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in results[:max(1, limit)]]


def _format_search_results(results: List[ToolSearchResult]) -> str:
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
    return "\n".join(lines)


def tool_search_func(query: str, limit: int = 10) -> str:
    """搜索可用工具并返回匹配结果。

    当模型不确定使用哪个工具时，调用此函数按关键字搜索。
    匹配的工具将获得完整 schema 并可正常调用。
    """
    results = _search_tools(query, limit=limit)
    return _format_search_results(results)


def _get_tool_detail(name: str) -> Optional[Dict[str, Any]]:
    """获取工具的详细信息（供 ToolSearch 确认后返回完整定义）。"""
    meta = get_tool_meta(name)
    if meta is None:
        return None
    return {
        "name": meta.name,
        "description": meta.description,
        "group": meta.tool_group,
        "is_read_only": meta.is_read_only,
        "is_destructive": meta.is_destructive,
        "is_concurrency_safe": meta.is_concurrency_safe,
        "requires_confirmation": meta.requires_confirmation,
        "search_hint": meta.search_hint,
        "should_defer": meta.should_defer,
    }


def create_tool_search_tool() -> StructuredTool:
    """创建 ToolSearch LangChain 工具实例。"""
    return StructuredTool.from_function(
        func=tool_search_func,
        name="ToolSearch",
        description=(
            "搜索可用工具。当你不确定使用哪个工具来完成任务时，"
            "先用关键字搜索匹配的工具名。返回最佳匹配的工具列表。"
            "对于标记为 '延迟加载' 的工具，搜索后可使用其完整定义。"
        ),
        args_schema=ToolSearchInput,
    )


__all__ = [
    "ToolSearchInput",
    "ToolSearchResult",
    "create_tool_search_tool",
    "tool_search_func",
    "_search_tools",
    "_get_tool_detail",
]
