"""进程内集成验证真实协议适配器，不走网络不依赖服务商。"""

from __future__ import annotations

import json

from fastmcp import FastMCP
from langchain.mcp import MCPAdapter
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from sayacode.extensions.mcp import MCPRegistry
from tests.support import ContractModel, contract_app


async def test_read_only_start_defers_mcp_connection_until_trust_changes(
    tmp_path, monkeypatch
) -> None:
    original = MCPRegistry.reload
    calls = 0

    async def count_reload(registry):
        nonlocal calls
        calls += 1
        return await original(registry)

    monkeypatch.setattr(MCPRegistry, "reload", count_reload)
    app = await contract_app(tmp_path, trust_level="read_only")
    try:
        assert calls == 0
        await app._get_handle(thread_id=app.session_id, trust_level="read_only")
        assert calls == 0
        await app._save_thread_policy(app.session_id, trust_level="ask")
        await app._get_handle(thread_id=app.session_id, trust_level="ask")
        assert calls == 1
    finally:
        await app.aclose()


class LookupOptions(BaseModel):
    tags: list[str]
    limit: int = 2


class LookupResult(BaseModel):
    query: str
    tags: list[str]
    total: int


async def test_official_adapter_preserves_nested_schema_and_structured_artifact():
    server = FastMCP("contract-server")

    @server.tool
    async def lookup(query: str, options: LookupOptions) -> LookupResult:
        return LookupResult(query=query, tags=options.tags, total=options.limit)

    async with MCPAdapter(server) as adapter:
        tools = await adapter.list_tools()
        assert [tool.name for tool in tools] == ["lookup"]
        schema = tools[0].args_schema
        assert schema["properties"]["options"]["type"] == "object"
        assert schema["properties"]["options"]["properties"]["tags"]["type"] == "array"
        result = await tools[0].ainvoke(
            {
                "name": "lookup",
                "type": "tool_call",
                "id": "mcp-lookup",
                "args": {"query": "code", "options": {"tags": ["python"], "limit": 3}},
            }
        )
        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == "mcp-lookup"
        assert result.artifact["structured_content"] == {
            "query": "code",
            "tags": ["python"],
            "total": 3,
        }
        assert any(block["type"] == "text" for block in result.content)


async def test_project_mcp_trust_approval_and_untrust_use_the_official_adapter(
    tmp_path, monkeypatch
):
    server = FastMCP("trusted-project-server")
    calls = []

    @server.tool
    async def lookup(value: str) -> dict:
        calls.append(value)
        return {"value": value}

    adapter_targets = []

    def in_process_transport(target):
        adapter_targets.append(target)
        return MCPAdapter(server)

    monkeypatch.setattr("langchain.mcp.MCPAdapter", in_process_transport)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".mcp.json").write_text(
        json.dumps({"mcpServers": {"contract": {"command": "in-process-test"}}}), encoding="utf-8"
    )
    model = ContractModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "mcp__lookup",
                        "args": {"value": "requested"},
                        "id": "remote-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="lookup complete"),
        ]
    )
    app = await contract_app(tmp_path, model)
    try:
        assert app.mcp.trusted is False
        assert app.mcp.tools == []
        assert adapter_targets == []
        app.config.trusted_mcp_projects = [str(workspace)]
        await app.repository.save(app.config)
        await app.mcp.reload()
        assert app.mcp.trusted is True
        assert [item.name for item in app.mcp.tools] == ["mcp__lookup"]
        assert len(adapter_targets) == 1
        paused = await app.run("Look up the requested value")
        assert paused["status"] == "paused"
        assert calls == []
        result = await app._resume_approval(
            "approve", {"thread_id": app.session_id, "decisions": [{"type": "approve"}]}
        )
        assert result["status"] == "completed"
        assert calls == ["requested"]
        app.config.trusted_mcp_projects = []
        await app.repository.save(app.config)
        await app.mcp.reload()
        assert app.mcp.trusted is False
        assert app.mcp.tools == []
        assert len(adapter_targets) == 1
        assert str(workspace) not in (await app.repository.load()).trusted_mcp_projects
    finally:
        await app.aclose()
