"""联网搜索工具。

默认 provider 有意避免 API key 和付费服务。它把 DuckDuckGo 的轻量 HTML 结果页
作为尽力而为的搜索来源，并可选地通过环境变量支持用户自建的 SearXNG 实例。
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from langchain_core.tools import tool

from ..core.permissions import enforce_tool_permission


DEFAULT_TIMEOUT = 15
MAX_RESULTS_LIMIT = 10
USER_AGENT = "SAYACODE/1.0 (+https://github.com/saya-ch/sayacode)"


@dataclass
class SearchResult:
    """单条网页搜索结果。"""
    title: str
    url: str
    snippet: str = ""


class DuckDuckGoHTMLParser(HTMLParser):
    """从 DuckDuckGo HTML/Lite 结果中提取标题、链接和摘要。"""

    def __init__(self) -> None:
        """初始化解析器状态并清空结果缓存。"""
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """识别结果标题与摘要标签并开始采集文本。"""
        attr_map = {name: value or "" for name, value in attrs}
        class_name = attr_map.get("class", "")
        if tag == "a" and ("result__a" in class_name or "result-link" in class_name):
            self._capture = "title"
            self._buffer = []
            self._href = attr_map.get("href", "")
        elif "result__snippet" in class_name or "result-snippet" in class_name:
            self._capture = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        """采集标题与摘要标签内的文本片段。"""
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        """闭合标签时落盘单条标题或摘要。"""
        if not self._capture:
            return
        text = _clean_text(" ".join(self._buffer))
        if self._capture == "title" and tag == "a":
            url = _normalize_duckduckgo_url(self._href)
            if text and url:
                self.results.append(SearchResult(title=text, url=url))
            self._reset_capture()
        elif self._capture == "snippet" and tag in {"a", "div", "td"}:
            if text and self.results and not self.results[-1].snippet:
                self.results[-1].snippet = text
            self._reset_capture()

    def _reset_capture(self) -> None:
        self._capture = None
        self._buffer = []
        self._href = ""


@tool
def web_search(
    query: str,
    max_results: int = 5,
    region: str = "wt-wt",
    time_range: str = "",
) -> str:
    """
    无需 API key 即可搜索公开网页。

    参数:
        query: 搜索关键词。
        max_results: 返回结果数量，取值 1 到 10。
        region: DuckDuckGo 区域代码，例如 "wt-wt"、"us-en"、"cn-zh"。
        time_range: 可选的新鲜度过滤："day"、"week"、"month" 或 "year"。

    返回:
        包含标题、URL 和摘要的纯文本搜索结果。
    """
    permission_error = enforce_tool_permission(
        "web_search",
        {"query": query, "max_results": max_results},
    )
    if permission_error:
        return permission_error

    query = _clean_text(query)
    if not query:
        return "Web search failed: query is empty."

    max_results = _clamp_max_results(max_results)
    provider = os.environ.get("SAYACODE_SEARCH_PROVIDER", "duckduckgo").strip().lower()
    try:
        if provider == "searxng":
            results = _search_searxng(query, max_results=max_results, time_range=time_range)
            provider_label = "searxng"
        else:
            results = _search_duckduckgo(
                query,
                max_results=max_results,
                region=region,
                time_range=time_range,
            )
            provider_label = "duckduckgo"
    except Exception as exc:
        return f"Web search failed: {exc}"

    if not results:
        return f"No web search results found for: {query}"
    return _format_results(query, provider_label, results[:max_results])


def _search_duckduckgo(
    query: str,
    *,
    max_results: int,
    region: str,
    time_range: str,
) -> list[SearchResult]:
    params = {
        "q": query,
        "kl": region or "wt-wt",
    }
    ddg_time = _duckduckgo_time_range(time_range)
    if ddg_time:
        params["df"] = ddg_time
    url = "https://html.duckduckgo.com/html/?" + urlencode(params)
    html = _http_get_text(url)
    results = _parse_duckduckgo_results(html, max_results=max_results)
    if results:
        return results

    lite_url = "https://lite.duckduckgo.com/lite/?" + urlencode(params)
    html = _http_get_text(lite_url)
    return _parse_duckduckgo_results(html, max_results=max_results)


def _parse_duckduckgo_results(html: str, *, max_results: int) -> list[SearchResult]:
    parser = DuckDuckGoHTMLParser()
    parser.feed(html)
    return _dedupe_results(parser.results, max_results=max_results)


def _search_searxng(query: str, *, max_results: int, time_range: str) -> list[SearchResult]:
    base_url = os.environ.get("SAYACODE_SEARXNG_URL", "").strip().rstrip("/")
    if not base_url:
        raise ValueError("SAYACODE_SEARXNG_URL is required when SAYACODE_SEARCH_PROVIDER=searxng")
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "safesearch": "1",
    }
    searx_time = _searxng_time_range(time_range)
    if searx_time:
        params["time_range"] = searx_time
    payload = json.loads(_http_get_text(f"{base_url}/search?{urlencode(params)}"))
    raw_results = payload.get("results", [])
    results = [
        SearchResult(
            title=_clean_text(str(item.get("title", ""))),
            url=_clean_text(str(item.get("url", ""))),
            snippet=_clean_text(str(item.get("content", ""))),
        )
        for item in raw_results
        if item.get("title") and item.get("url")
    ]
    return _dedupe_results(results, max_results=max_results)


def _http_get_text(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from search provider") from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def _normalize_duckduckgo_url(href: str) -> str:
    href = unescape(href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return _clean_text(target)
    return _clean_text(href)


def _dedupe_results(results: list[SearchResult], *, max_results: int) -> list[SearchResult]:
    deduped: list[SearchResult] = []
    seen: set[str] = set()
    for result in results:
        url = result.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(SearchResult(
            title=_clean_text(result.title),
            url=url,
            snippet=_clean_text(result.snippet),
        ))
        if len(deduped) >= max_results:
            break
    return deduped


def _format_results(query: str, provider: str, results: list[SearchResult]) -> str:
    lines = [f"Web search results for: {query}", f"Provider: {provider}", ""]
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result.title}")
        lines.append(f"   URL: {result.url}")
        if result.snippet:
            lines.append(f"   Snippet: {result.snippet}")
        lines.append("")
    return "\n".join(lines).strip()


def _clean_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _clamp_max_results(value: int | str | None) -> int:
    if value is None:
        return 5
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 5
    return max(1, min(MAX_RESULTS_LIMIT, number))


def _duckduckgo_time_range(value: str) -> str:
    mapping = {"day": "d", "week": "w", "month": "m", "year": "y"}
    return mapping.get(str(value or "").strip().lower(), "")


def _searxng_time_range(value: str) -> str:
    mapping = {"day": "day", "week": "week", "month": "month", "year": "year"}
    return mapping.get(str(value or "").strip().lower(), "")


__all__ = [
    "SearchResult",
    "DuckDuckGoHTMLParser",
    "web_search",
]
