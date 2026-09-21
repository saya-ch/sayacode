"""委派保持可调用，服务商使用 JSON 工具结构。"""

from __future__ import annotations

from tests.support import contract_app


async def test_delegation_runtime_is_injected_outside_provider_schema(tmp_path) -> None:
    app = await contract_app(tmp_path)
    try:
        delegate = next(item for item in app._team_tools() if item.name == "delegate_to_subagent")
        schema = delegate.tool_call_schema.model_json_schema()
        assert set(schema["properties"]) == {"task", "role"}
        assert "runtime" not in schema["properties"]
    finally:
        await app.aclose()
