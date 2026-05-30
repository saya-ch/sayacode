import json

from lib.core.permissions import READ_ONLY_TOOLS
from lib.core.tool_meta import get_tool_meta
from lib.tools.registry import ToolFactory
from lib.runtime.context import RuntimeContext
from lib.tools.web_tools import (
    _clean_text,
    _search_duckduckgo,
    _search_searxng,
    web_search,
)


DUCK_HTML = """
<html>
  <body>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Example Docs</a>
    <a class="result__snippet">Useful docs &amp; examples.</a>
    <a class="result__a" href="https://example.org/blog">Example Blog</a>
    <div class="result__snippet">A blog result.</div>
  </body>
</html>
"""


def test_duckduckgo_html_parser_extracts_results(monkeypatch):
    requested_urls = []

    def fake_get(url):
        requested_urls.append(url)
        return DUCK_HTML

    monkeypatch.setattr("lib.tools.web_tools._http_get_text", fake_get)

    results = _search_duckduckgo("python testing", max_results=5, region="us-en", time_range="week")

    assert "q=python+testing" in requested_urls[0]
    assert "kl=us-en" in requested_urls[0]
    assert "df=w" in requested_urls[0]
    assert len(results) == 2
    assert results[0].title == "Example Docs"
    assert results[0].url == "https://example.com/docs"
    assert results[0].snippet == "Useful docs & examples."


def test_web_search_formats_results_without_api_key(monkeypatch):
    monkeypatch.delenv("SAYACODE_SEARCH_PROVIDER", raising=False)
    monkeypatch.setattr("lib.tools.web_tools._http_get_text", lambda url: DUCK_HTML)

    output = web_search.invoke({"query": "sayacode", "max_results": 1})

    assert "Web search results for: sayacode" in output
    assert "Provider: duckduckgo" in output
    assert "1. Example Docs" in output
    assert "https://example.com/docs" in output
    assert "2. Example Blog" not in output


def test_searxng_provider_uses_optional_env_config(monkeypatch):
    payload = {
        "results": [
            {"title": "Result", "url": "https://example.com", "content": "Snippet"},
        ]
    }
    requested_urls = []

    def fake_get(url):
        requested_urls.append(url)
        return json.dumps(payload)

    monkeypatch.setenv("SAYACODE_SEARXNG_URL", "https://search.example")
    monkeypatch.setattr("lib.tools.web_tools._http_get_text", fake_get)

    results = _search_searxng("open source search", max_results=5, time_range="month")

    assert requested_urls[0].startswith("https://search.example/search?")
    assert "format=json" in requested_urls[0]
    assert "time_range=month" in requested_urls[0]
    assert results[0].title == "Result"
    assert results[0].url == "https://example.com"


def test_web_search_is_registered_as_read_only_runtime_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    context = RuntimeContext(
        workspace=tmp_path,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )

    names = {tool.name for tool in ToolFactory(context)}
    meta = get_tool_meta("web_search")

    assert "web_search" in names
    assert meta is not None
    assert meta.is_read_only is True
    assert meta.is_concurrency_safe is True
    assert "web_search" in READ_ONLY_TOOLS


def test_clean_text_collapses_control_characters():
    assert _clean_text(" A\n\tB\x00 C ") == "A B C"
