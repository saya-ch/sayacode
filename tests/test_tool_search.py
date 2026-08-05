"""P2: ToolSearch 工具测试."""

import pytest
from lib.tools.tool_search import (
    _search_tools,
    _format_search_results,
    ToolSearchResult,
    TOOL_SEARCH_MAX_SCHEMA_CHARS,
    tool_search_func,
    create_tool_search_tool,
    create_deferred_tool_invoke_tool,
)
from lib.core.tool_meta import ToolMeta, get_tool_meta, register_tool_meta
from langchain_core.tools import StructuredTool


@pytest.fixture(autouse=True)
def _register_test_metas():
    metas = [
        ToolMeta.safe_default("read_file", is_read_only=True, tool_group="file",
                              search_hint="read file contents by path"),
        ToolMeta.safe_default("write_file", tool_group="file",
                              search_hint="create or overwrite a file"),
        ToolMeta.safe_default("execute_command_tool", tool_group="shell",
                              search_hint="run shell commands in terminal"),
        ToolMeta.safe_default("git_status", is_read_only=True, tool_group="git",
                              search_hint="show working tree status"),
        ToolMeta.safe_default("git_commit", tool_group="git",
                              search_hint="record changes to the repository"),
        ToolMeta.safe_default("analyze_project", is_read_only=True, tool_group="project",
                              search_hint="scan and analyze project structure"),
    ]
    previous = {meta.name: get_tool_meta(meta.name) for meta in metas}
    for meta in metas:
        register_tool_meta(meta)
    yield
    for name, meta in previous.items():
        if meta is not None:
            register_tool_meta(meta)


class TestSearchTools:
    def test_exact_name_match(self):
        results = _search_tools("read_file")
        assert len(results) > 0
        assert results[0].name == "read_file"
        assert "精确名称匹配" in results[0].match_reason

    def test_prefix_match(self):
        results = _search_tools("git_")
        assert len(results) >= 2
        names = {r.name for r in results}
        assert "git_status" in names
        assert "git_commit" in names

    def test_search_hint_match(self):
        results = _search_tools("scan")
        assert len(results) > 0
        names = {r.name for r in results}
        assert "analyze_project" in names

    def test_group_match(self):
        results = _search_tools("shell")
        assert len(results) > 0
        assert all(r.group == "shell" for r in results)

    def test_partial_word_match(self):
        results = _search_tools("file contents")
        assert len(results) > 0
        # read_file should match because of "read file contents"
        names = {r.name for r in results}
        assert "read_file" in names

    def test_no_match(self):
        results = _search_tools("xyzzy_nonexistent_12345")
        assert results == []

    def test_limit(self):
        results = _search_tools("git", limit=1)
        assert len(results) <= 1

    def test_format_results(self):
        results = [ToolSearchResult(
            name="read_file", group="file",
            match_reason="精确名称匹配", description="",
            search_hint="read file contents", is_deferred=False,
        )]
        formatted = _format_search_results(results)
        assert "read_file" in formatted
        assert "精确名称匹配" in formatted

    def test_format_no_results(self):
        formatted = _format_search_results([])
        assert "未找到" in formatted

    def test_format_truncates_untrusted_large_schema(self):
        results = [ToolSearchResult(
            name="deferred_probe",
            group="mcp",
            match_reason="精确名称匹配",
            description="probe",
            search_hint="probe",
            is_deferred=True,
        )]
        formatted = _format_search_results(results, {
            "deferred_probe": {
                "input_schema": {"description": "x" * (TOOL_SEARCH_MAX_SCHEMA_CHARS + 100)},
            }
        })

        assert "[schema truncated]" in formatted
        assert len(formatted) < TOOL_SEARCH_MAX_SCHEMA_CHARS + 500


class TestToolSearchFunc:
    def test_search_returns_string(self):
        result = tool_search_func("read")
        assert isinstance(result, str)
        assert "read_file" in result

    def test_search_no_match(self):
        result = tool_search_func("nonexistent_xyz")
        assert isinstance(result, str)


class TestCreateToolSearchTool:
    def test_creates_langchain_tool(self):
        tool = create_tool_search_tool()
        assert tool.name == "ToolSearch"
        assert tool.description is not None
        assert "搜索" in tool.description
        assert tool.args_schema is not None

    def test_runtime_search_includes_deferred_schema(self):
        register_tool_meta(ToolMeta.safe_default(
            "deferred_probe",
            is_read_only=True,
            should_defer=True,
            tool_group="project",
            search_hint="inspect deferred probe",
        ))
        deferred = StructuredTool.from_function(
            func=lambda value: f"value={value}",
            name="deferred_probe",
            description="Inspect a deferred probe value.",
        )
        tool = create_tool_search_tool([deferred])

        result = tool.invoke({"query": "deferred_probe"})

        assert "参数 schema" in result
        assert "invoke_tool" in result
        assert "value" in result

    def test_runtime_search_excludes_registered_but_unavailable_tools(self):
        register_tool_meta(ToolMeta.safe_default(
            "stale_deferred_probe",
            should_defer=True,
            search_hint="stale unavailable probe",
        ))
        available = StructuredTool.from_function(
            func=lambda value: value,
            name="deferred_probe",
            description="Available deferred probe.",
        )
        tool = create_tool_search_tool([available])

        result = tool.invoke({"query": "stale_deferred_probe"})

        assert "未找到" in result

    def test_deferred_invoker_calls_bound_tool(self):
        deferred = StructuredTool.from_function(
            func=lambda value: f"value={value}",
            name="deferred_probe",
            description="Inspect a deferred probe value.",
        )
        invoker = create_deferred_tool_invoke_tool([deferred])

        result = invoker.invoke({
            "tool_name": "deferred_probe",
            "arguments": {"value": "ok"},
        })

        assert result == "value=ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
