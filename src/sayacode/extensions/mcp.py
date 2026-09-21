"""外部模型上下文服务命名与进程资源管理。

远端工具统一加前缀并保留来源信息，避免命名冲突。
大输出转存文件以保护上下文窗口，原生消息结构不变。
项目级服务默认不可信，需显式信任后才启动进程。"""

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
    """为远端工具加统一前缀并保留原始信息。

    参数为适配器返回的工具列表。返回改名后的新列表。
    分三步处理。先清洗非法字符，再对过长或改动过的名字加哈希后缀。
    最后写入来源标记并检查重名，重名直接抛错。
    约束是不修改原对象，返回均为拷贝。
    坑点是不同原名可能收敛到同一名字，需靠抛错发现。"""
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
    """远端工具大输出转存中间件，只处理带前缀的调用。

    超限文本写文件并保留预览，图片与音频类负载原样放行。
    阈值由构造参数决定，目录不存在时会自动创建。
    同一内容复用同一文件，避免重复写入。"""

    def __init__(self, output_dir: Path, limit: int = 64 * 1024) -> None:
        """创建转存中间件并记录阈值与目录。

        参数为输出目录与字节阈值。无返回值。
        目录此时不创建，首次超限写入时才创建。
        坑点是阈值按字节比较而非字符数。"""
        super().__init__()
        self.output_dir = output_dir
        self.limit = limit

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        """包装远端工具调用，超限结果转存为文件引用。

        参数为调用请求与后继处理器。返回处理后的原生消息。
        先执行真实工具，非远端调用或非消息结果直接返回。
        再对内容与附带产物分别做转存，保持消息类型不变。
        约束是只识别带统一前缀的工具名。
        坑点是转存只缩小文本，多媒体负载不受阈值影响。"""
        result = await handler(request)
        if not str(request.tool_call.get("name") or "").startswith("mcp__"):
            return result
        if not isinstance(result, ToolMessage):
            return result
        content = await self._spill_value(result.content)
        artifact = await self._spill_value(result.artifact)
        return result.model_copy(update={"content": content, "artifact": artifact})

    async def _spill_value(self, value: Any) -> Any:
        """递归转存超限文本，结构形状保持不变。

        分三类处理。字符串超限则写文件并返回预览加路径。
        列表与字典逐项递归，多媒体类型直接返回。
        其他类型原样返回，不做转换。
        文件写入放在线程中执行，避免阻塞事件循环。
        坑点是预览按字节截取，可能切断多字节字符尾部。"""
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) <= self.limit:
                return value
            target = self.output_dir / f"mcp-{hashlib.sha256(encoded).hexdigest()}.txt"

            def write() -> None:
                """写 MCP 服务端配置。"""
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
    """远端服务注册表，集中管理进程工具与信任状态。

    主工作区进程常驻后台栈，子工作区进程按需缓存。
    项目级配置仅在受信后合并，用户级配置始终生效。
    重载会关闭旧进程并重建工具列表，调用方需刷新引用。
    信任变更后必须重载，新状态才会真正生效。"""

    def __init__(
        self,
        workspace: Path,
        config: Config,
        save_config: Callable[[], Awaitable[None]],
        invalidate: Callable[[], None],
    ) -> None:
        """创建注册表并记录依赖回调。

        参数为工作区与全局配置。保存回调用于持久化信任变更。
        失效回调用于通知上层刷新工具列表。
        约束是构造时不启动任何进程，首次重载才连接。
        坑点是回调必须由调用方保证线程安全与可重入。"""
        self.workspace = workspace
        self.config = config
        self.save_config = save_config
        self.invalidate = invalidate
        self._stack = AsyncExitStack()
        self._task_stacks: dict[str, tuple[AsyncExitStack, list[BaseTool]]] = {}
        self.tools: list[BaseTool] = []
        self.error: str | None = None

    async def close(self) -> None:
        """关闭全部远端进程栈。

        无参数输入，无返回值。
        先关子工作区缓存，再关主工作区常驻栈。
        调用约束是关闭后实例仍可重载，不可复用旧工具。
        坑点是关闭顺序与创建顺序相反，避免残留连接。"""
        for stack, _ in self._task_stacks.values():
            await stack.aclose()
        await self._stack.aclose()

    def _project_servers(self, workspace: Path | None = None) -> dict[str, dict[str, Any]]:
        """读取项目级服务声明，缺失或损坏时返回空字典。"""
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
        """判断当前工作区是否已受信。

        无参数输入。返回是否在信任名单中。
        只读判断，不触发进程启停。
        坑点是未受信时项目配置会被整体忽略。"""
        return str(self.workspace) in set(self.config.trusted_mcp_projects)

    def _config_for(self, workspace: Path) -> dict[str, dict[str, Any]]:
        """合并用户级与项目级服务，项目级同名覆盖用户级。

        未受信时只返回用户级。含启动命令的服务会补工作目录。
        返回的新字典可直接交给适配器，不会污染原始配置。"""
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
        """返回指定工作区的工具，主工作区走常驻缓存。

        参数为目标工作区路径。返回该空间下的工具列表。
        分三步执行。主工作区直接返回常驻列表。
        子工作区先查缓存，命中则直接返回。
        未命中则按合并后配置新建进程栈并缓存。
        约束是空配置直接返回空，不启动进程。
        坑点是新建失败会关闭刚建的栈，不会留下半开进程。"""
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
        """重建主工作区全部进程并刷新工具列表。

        无参数输入。返回重建后的工具列表副本。
        分四步执行。先关闭子空间缓存，再关闭常驻栈并清空状态。
        然后检查项目配置信任情况，未受信只记错误不启动项目服务。
        最后连接合并后配置，失败则记录错误并返回空。
        约束是无论成功失败都会通知上层刷新。
        坑点是旧工具引用在重载后即失效，必须重新获取。"""
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
        """解析斜杠命令文本并执行服务管理动作。

        参数为原始参数文本或空。返回各动作对应的状态字典。
        支持查看状态与受信解信。支持重载与增删服务。
        增删改后会自动保存配置并重载进程。
        约束是未知动作直接抛错并提示可用动作。
        坑点是增删只改用户级配置，不触碰项目级文件。"""
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
