"""Native MCP image content stays multimodal through the official adapter."""

from __future__ import annotations

import base64

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from langchain.mcp import MCPAdapter
from langchain_core.messages import ToolMessage


async def test_mcp_image_is_not_flattened_to_text() -> None:
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9xUpQAAAAASUVORK5CYII="
    )
    server = FastMCP("image")

    @server.tool
    def preview() -> Image:
        return Image(data=pixel, format="png")

    async with MCPAdapter(server) as adapter:
        image_tool = (await adapter.list_tools())[0]
        message = await image_tool.ainvoke(
            {"name": "preview", "args": {}, "id": "image-call", "type": "tool_call"}
        )
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "image-call"
    assert any(
        isinstance(block, dict) and block.get("type") == "image" for block in message.content
    )
