"""MCP 原生工具命名及服务资源。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from ..config import Config


def namespace_mcp_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """保留官方结构和调用方式。给远端工具预留命名空间。"""
    result = []
    names: set[str] = set()
    for item in tools:
        original = item.name
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", original)
        if safe != original or len(safe) > 59:
            safe = safe[:46] + "_" + hashlib.sha256(original.encode()).hexdigest()[:12]
        name = f"mcp__{safe}"
        if name in names:
            raise ValueError(f"Duplicate MCP tool name: {original}")
        names.add(name)
        metadata = {
            **(item.metadata or {}),
            "sayacode_origin": "mcp",
            "mcp_original_name": original,
        }
        result.append(item.model_copy(update={"name": name, "metadata": metadata}))
    return result


class MCPOutputMiddleware(AgentMiddleware):
    """大文本结果转存文件。保持原生消息不变。"""

    def __init__(self, output_dir: Path, limit: int = 64 * 1024) -> None:
        super().__init__()
        self.output_dir = output_dir
        self.limit = limit

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        result = await handler(request)
        if not str(request.tool_call.get("name") or "").startswith("mcp__"):
            return result
        if not isinstance(result, ToolMessage):
            return result
        content = await self._spill_value(result.content)
        artifact = await self._spill_value(result.artifact)
        return result.model_copy(update={"content": content, "artifact": artifact})

    async def _spill_value(self, value: Any) -> Any:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) <= self.limit:
                return value
            target = self.output_dir / f"mcp-{hashlib.sha256(encoded).hexdigest()}.txt"

            def write() -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(encoded)

            await asyncio.to_thread(write)
            preview = encoded[: self.limit].decode("utf-8", "ignore")
            return f"{preview}\n[Full MCP output: {target}; {len(encoded)} bytes]"
        if isinstance(value, list):
            return [await self._spill_value(item) for item in value]
        if isinstance(value, dict):
            if value.get("type") in {"image", "image_url", "audio", "audio_url"}:
                return value
            return {key: await self._spill_value(item) for key, item in value.items()}
        return value


class MCPRegistry:
    """集中持有 MCP 服务进程、工具和项目信任状态。"""

    def __init__(
        self,
        workspace: Path,
        config: Config,
        save_config: Callable[[], Awaitable[None]],
        invalidate: Callable[[], None],
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.save_config = save_config
        self.invalidate = invalidate
        self._stack = AsyncExitStack()
        self._task_stacks: dict[str, tuple[AsyncExitStack, list[BaseTool]]] = {}
        self.tools: list[BaseTool] = []
        self.error: str | None = None

    async def close(self) -> None:
        for stack, _ in self._task_stacks.values():
            await stack.aclose()
        await self._stack.aclose()

    def _project_servers(self, workspace: Path | None = None) -> dict[str, dict[str, Any]]:
        path = (workspace or self.workspace) / ".mcp.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        servers = raw.get("mcpServers", raw) if isinstance(raw, dict) else {}
        return {
            str(name): dict(value) for name, value in servers.items() if isinstance(value, dict)
        }

    @property
    def trusted(self) -> bool:
        return str(self.workspace) in set(self.config.trusted_mcp_projects)

    def _config_for(self, workspace: Path) -> dict[str, dict[str, Any]]:
        project = self._project_servers(workspace) if self.trusted else {}
        servers = {**self.config.mcp_servers, **project}
        configured: dict[str, dict[str, Any]] = {}
        for name, server in servers.items():
            value = dict(server)
            if "command" in value:
                value["cwd"] = str(workspace)
            configured[name] = value
        return configured

    async def tools_for_workspace(self, workspace: Path) -> list[BaseTool]:
        if workspace == self.workspace:
            return self.tools
        key = str(workspace)
        cached = self._task_stacks.get(key)
        if cached is not None:
            return cached[1]
        servers = self._config_for(workspace)
        if not servers:
            return []
        from langchain.mcp import MCPAdapter

        stack = AsyncExitStack()
        try:
            adapter = MCPAdapter({"mcpServers": servers})
            await stack.enter_async_context(adapter)
            tools = namespace_mcp_tools(await adapter.list_tools())
        except BaseException:
            await stack.aclose()
            raise
        self._task_stacks[key] = (stack, tools)
        return tools

    async def reload(self) -> list[BaseTool]:
        for stack, _ in self._task_stacks.values():
            await stack.aclose()
        self._task_stacks.clear()
        await self._stack.aclose()
        self._stack = AsyncExitStack()
        self.tools = []
        self.error = None
        project_servers = self._project_servers()
        if project_servers and not self.trusted:
            self.error = "Project MCP configuration is untrusted; run /mcp trust first"
        servers = self._config_for(self.workspace)
        if not servers:
            self.invalidate()
            return []
        try:
            from langchain.mcp import MCPAdapter

            adapter = MCPAdapter({"mcpServers": servers})
            await self._stack.enter_async_context(adapter)
            self.tools = namespace_mcp_tools(await adapter.list_tools())
        except Exception as exc:
            self.error = str(exc)
            self.tools = []
        self.invalidate()
        return list(self.tools)

    async def command(self, args: Any) -> Any:
        tokens = shlex.split(str(args or ""))
        action = tokens[0].lower() if tokens else "status"
        if action == "status":
            return {
                "trusted": self.trusted,
                "project_servers": sorted(self._project_servers()),
                "user_servers": sorted(self.config.mcp_servers),
                "tools": [
                    {"name": tool.name, "description": tool.description} for tool in self.tools
                ],
                "error": self.error,
            }
        if action == "trust":
            trusted = set(self.config.trusted_mcp_projects)
            trusted.add(str(self.workspace))
            self.config.trusted_mcp_projects = sorted(trusted)
            await self.save_config()
            await self.reload()
            return {"trusted": True, "tools": [tool.name for tool in self.tools]}
        if action == "untrust":
            trusted = set(self.config.trusted_mcp_projects)
            trusted.discard(str(self.workspace))
            self.config.trusted_mcp_projects = sorted(trusted)
            await self.save_config()
            await self.reload()
            return {"trusted": False}
        if action == "reload":
            await self.reload()
            return {"tools": [tool.name for tool in self.tools], "error": self.error}
        if action == "add":
            if len(tokens) < 3:
                raise ValueError("Usage: /mcp add <name> <command> [arguments...]")
            name, executable = tokens[1:3]
            self.config.mcp_servers[name] = {"command": executable, "args": tokens[3:]}
            await self.save_config()
            await self.reload()
            return {"added": name, "error": self.error}
        if action in {"remove", "delete"}:
            if len(tokens) != 2:
                raise ValueError("Usage: /mcp remove <name>")
            self.config.mcp_servers.pop(tokens[1], None)
            await self.save_config()
            await self.reload()
            return {"removed": tokens[1]}
        raise ValueError("Usage: /mcp [status|trust|untrust|reload|add|remove]")
