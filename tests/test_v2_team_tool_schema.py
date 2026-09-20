"""Delegation stays callable by providers using JSON tool schemas."""

from __future__ import annotations

from test_v2_product_contracts import contract_app


async def test_delegation_runtime_is_injected_outside_provider_schema(tmp_path) -> None:
    app = await contract_app(tmp_path)
    try:
        delegate = next(
            item for item in app._team_tools() if item.name == "delegate_to_subagent"
        )
        schema = delegate.tool_call_schema.model_json_schema()
        assert set(schema["properties"]) == {"task", "role"}
        assert "runtime" not in schema["properties"]
    finally:
        await app.aclose()
