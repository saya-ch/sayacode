"""通过 DDGS 或 SearXNG 查询网页。"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool

from .files import _limited


@tool
async def web_search(
    query: str,
    runtime: ToolRuntime[Any],
    max_results: int = 5,
    region: str = "wt-wt",
    time_range: Literal["", "day", "week", "month", "year"] = "",
) -> Any:
    """通过 DDGS 或已配置的 SearXNG 搜索，返回标题、链接和摘要。"""
    if not query.strip() or not 1 <= max_results <= 20:
        raise ValueError("query is required and max_results must be 1..20")
    searxng = os.environ.get("SAYACODE_SEARXNG_URL", "").rstrip("/")
    if os.environ.get("SAYACODE_SEARCH_PROVIDER", "ddgs") == "searxng":
        if not searxng:
            raise ValueError("SAYACODE_SEARXNG_URL is required")
        import httpx

        params = {"q": query, "format": "json", "language": region}
        if time_range:
            params["time_range"] = time_range
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(f"{searxng}/search", params=params)
            response.raise_for_status()
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in response.json().get("results", [])[:max_results]
        ]
    else:
        from ddgs import DDGS

        def search() -> list[dict[str, str]]:
            values = DDGS(timeout=20).text(
                query,
                region=region,
                timelimit={"day": "d", "week": "w", "month": "m", "year": "y"}.get(time_range),
                max_results=max_results,
            )
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("href", ""),
                    "snippet": item.get("body", ""),
                }
                for item in values
            ]

        results = await asyncio.to_thread(search)
    return _limited(results, runtime, "web")
