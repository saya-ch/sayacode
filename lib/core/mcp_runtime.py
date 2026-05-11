"""Minimal stdio MCP runtime.

This module implements the subset SAYACODE needs for local MCP tools:
process lifecycle, initialize, tools/list, and tools/call over JSON-RPC stdio.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Dict, Optional
import json
import re
import subprocess
import sys
import time
import atexit

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from .audit import append_audit_event
from .hooks import hook_runtime_session, trigger_hook_event
from .paths import SayacodePaths
from .permissions import enforce_tool_permission, permission_runtime_session
from .process_env import build_process_env
from .private_io import ensure_private_dir, write_private_json
from ..i18n import tr


MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_REQUEST_TIMEOUT = 10
MCP_MAX_OUTPUT = 10000


@dataclass(frozen=True)
class MCPServerConfig:
    """One configured stdio MCP server."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[Path] = None
    disabled: bool = False


@dataclass(frozen=True)
class MCPToolInfo:
    """One discovered MCP tool."""

    alias: str
    server_name: str
    name: str
    description: str
    input_schema: Dict[str, Any]


class MCPRuntimeError(RuntimeError):
    """MCP runtime failure."""


class MCPServerClient:
    """Synchronous JSON-RPC stdio client for one MCP server."""

    def __init__(self, config: MCPServerConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace
        self.process: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: Queue[str] = Queue()
        self._stderr_lines: list[str] = []
        self._next_id = 0
        self.tools: list[Dict[str, Any]] = []

    @property
    def active(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def start(self) -> None:
        if self.config.disabled:
            raise MCPRuntimeError("server is disabled")
        if self.active:
            return

        command = [self.config.command, *self.config.args]
        cwd = self.config.cwd or self.workspace
        env = build_process_env()
        env.update({key: str(value) for key, value in self.config.env.items()})
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "PIP_NO_INPUT": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        })

        self.process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
            **_popen_platform_kwargs(),
        )
        self._start_reader_threads()
        self._initialize()
        self.tools = self._request("tools/list", {}) .get("tools", [])

    def shutdown(self) -> None:
        process = self.process
        if not process or process.poll() is not None:
            return
        _terminate_process_tree(process)

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=MCP_REQUEST_TIMEOUT,
        )
        return _format_tool_result(result)

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.config.name,
            "active": self.active,
            "tools": len(self.tools),
            "stderr": "\n".join(self._stderr_lines[-5:]),
        }

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "sayacode", "version": "1.0.0"},
            },
            timeout=MCP_REQUEST_TIMEOUT,
        )
        self._notify("notifications/initialized", {})

    def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = MCP_REQUEST_TIMEOUT,
    ) -> Dict[str, Any]:
        process = self.process
        if not process or not process.stdin:
            raise MCPRuntimeError("server process is not running")

        self._next_id += 1
        request_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self._write_json(payload)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = "\n".join(self._stderr_lines[-5:])
                raise MCPRuntimeError(f"server exited with code {process.returncode}: {stderr}")

            try:
                line = self._stdout_queue.get(timeout=0.05)
            except Empty:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            if message.get("id") != request_id:
                continue

            if "error" in message:
                raise MCPRuntimeError(str(message["error"]))
            result = message.get("result", {})
            return result if isinstance(result, dict) else {"value": result}

        raise MCPRuntimeError(f"MCP request timed out: {method}")

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._write_json({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    def _write_json(self, payload: Dict[str, Any]) -> None:
        process = self.process
        if not process or not process.stdin:
            raise MCPRuntimeError("server stdin is unavailable")
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _start_reader_threads(self) -> None:
        process = self.process
        if not process or not process.stdout or not process.stderr:
            return

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                if line:
                    self._stdout_queue.put(line.strip())

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                if line:
                    self._stderr_lines.append(line.rstrip())
                    self._stderr_lines[:] = self._stderr_lines[-20:]

        Thread(target=read_stdout, daemon=True).start()
        Thread(target=read_stderr, daemon=True).start()


class MCPRuntime:
    """Workspace-scoped MCP process and tool registry."""

    def __init__(self, permissions: Optional[Any] = None, hooks: Optional[Any] = None) -> None:
        self.workspace: Optional[Path] = None
        self.config_path: Optional[Path] = None
        self.configured_servers: Dict[str, MCPServerConfig] = {}
        self.clients: Dict[str, MCPServerClient] = {}
        self.tools_by_alias: Dict[str, MCPToolInfo] = {}
        self.errors: Dict[str, str] = {}
        self.project_trusted = False
        self.permissions = permissions
        self.hooks = hooks

    def configure_workspace(self, workspace: str | Path) -> None:
        workspace_path = Path(workspace).expanduser().resolve()
        if self.workspace != workspace_path:
            self.shutdown()
        self.workspace = workspace_path
        self.config_path = workspace_path / ".mcp.json"
        self.project_trusted = is_mcp_workspace_trusted(workspace_path)
        self.configured_servers = _load_server_configs(workspace_path)
        self.errors = {}
        self.tools_by_alias = {}

    def load_tools(self, server_names: Optional[list[str]] = None) -> list[StructuredTool]:
        if self.workspace is None:
            self.configure_workspace(Path.cwd())

        self.shutdown()
        self.errors = {}
        self.tools_by_alias = {}

        if not self.project_trusted:
            if self.configured_servers:
                self.errors["trust"] = "Project MCP config exists but workspace is not trusted."
            return []

        selected_names = set(server_names or self.configured_servers.keys())
        tools: list[StructuredTool] = []

        for name, config in self.configured_servers.items():
            if name not in selected_names:
                continue
            try:
                client = MCPServerClient(config, self.workspace or Path.cwd())
                client.start()
                self.clients[name] = client
                for raw_tool in client.tools:
                    info = _build_tool_info(server_name=name, raw_tool=raw_tool)
                    self.tools_by_alias[info.alias] = info
                    tools.append(_build_langchain_tool(info, caller=self.call_tool))
            except Exception as exc:
                self.errors[name] = str(exc)
                append_audit_event(
                    "mcp",
                    "server_start_failed",
                    workspace=self.workspace,
                    allowed=False,
                    details={"server": name, "error": str(exc)},
                )

        return tools

    def call_tool(self, alias: str, arguments: Dict[str, Any]) -> str:
        with ExitStack() as stack:
            if self.permissions is not None:
                stack.enter_context(permission_runtime_session(self.permissions))
            if self.hooks is not None:
                stack.enter_context(hook_runtime_session(self.hooks))
            return self._call_tool(alias, arguments)

    def _call_tool(self, alias: str, arguments: Dict[str, Any]) -> str:
        info = self.tools_by_alias.get(alias)
        if not info:
            append_audit_event("mcp", alias, workspace=self.workspace, allowed=False, details={"error": "not_registered"})
            return f"❌ MCP tool is not registered: {alias}"

        block_reason = trigger_hook_event(
            "PreToolUse",
            {"tool_name": alias, "arguments": arguments, "mcp_server": info.server_name},
        )
        if block_reason:
            append_audit_event("mcp", alias, workspace=self.workspace, allowed=False, details={"reason": block_reason})
            return f"⚠️ {block_reason}"

        permission_error = enforce_tool_permission(
            alias,
            {"server": info.server_name, "tool": info.name, "arguments": arguments},
        )
        if permission_error:
            append_audit_event("mcp", alias, workspace=self.workspace, allowed=False, details={"reason": permission_error})
            return permission_error

        client = self.clients.get(info.server_name)
        if not client:
            append_audit_event(
                "mcp",
                alias,
                workspace=self.workspace,
                allowed=False,
                details={"server": info.server_name, "error": "server_not_running"},
            )
            return f"❌ MCP server is not running: {info.server_name}"

        try:
            result = client.call_tool(info.name, arguments)
        except Exception as exc:
            trigger_hook_event(
                "ToolFailure",
                {
                    "tool_name": alias,
                    "arguments": arguments,
                    "mcp_server": info.server_name,
                    "error": str(exc),
                },
            )
            append_audit_event(
                "mcp",
                alias,
                workspace=self.workspace,
                allowed=False,
                details={"server": info.server_name, "tool": info.name, "error": str(exc)},
            )
            return f"❌ MCP tool call failed: {exc}"

        trigger_hook_event(
            "PostToolUse",
            {
                "tool_name": alias,
                "arguments": arguments,
                "mcp_server": info.server_name,
                "result_preview": result[:1000],
            },
        )
        append_audit_event(
            "mcp",
            alias,
            workspace=self.workspace,
            allowed=True,
            details={"server": info.server_name, "tool": info.name, "result_preview": result[:500]},
        )
        return result

    def status(self) -> Dict[str, Any]:
        return {
            "workspace": str(self.workspace or ""),
            "config_path": str(self.config_path or ""),
            "trusted": self.project_trusted,
            "configured_servers": list(self.configured_servers),
            "active_servers": {
                name: client.status()
                for name, client in self.clients.items()
            },
            "tools": [
                {
                    "alias": info.alias,
                    "server": info.server_name,
                    "name": info.name,
                    "description": info.description,
                }
                for info in self.tools_by_alias.values()
            ],
            "errors": dict(self.errors),
        }

    def shutdown(self) -> None:
        for client in list(self.clients.values()):
            client.shutdown()
        self.clients.clear()


def configure_mcp_workspace(workspace: str | Path) -> None:
    """Configure the global MCP runtime for workspace."""
    _RUNTIME.configure_workspace(workspace)


def load_mcp_tools(server_names: Optional[list[str]] = None) -> list[StructuredTool]:
    """Start trusted MCP servers and return LangChain tools."""
    return _RUNTIME.load_tools(server_names=server_names)


def reload_mcp_tools(server_names: Optional[list[str]] = None) -> list[StructuredTool]:
    """Restart MCP servers and rediscover tools."""
    return _RUNTIME.load_tools(server_names=server_names)


def call_mcp_tool(alias: str, arguments: Optional[Dict[str, Any]] = None) -> str:
    """Call one registered MCP tool by SAYACODE alias."""
    return _RUNTIME.call_tool(alias, arguments or {})


def get_mcp_status() -> Dict[str, Any]:
    """Return global MCP runtime status."""
    return _RUNTIME.status()


def shutdown_mcp_runtime() -> None:
    """Stop all MCP server processes."""
    _RUNTIME.shutdown()


def trust_mcp_workspace(workspace: str | Path) -> Path:
    """Trust project MCP config for one workspace."""
    workspace_path = Path(workspace).expanduser().resolve()
    path = _trusted_mcp_projects_path(create=True)
    data = _read_json_file(path) or {"workspaces": []}
    workspaces = data.setdefault("workspaces", [])
    workspace_text = str(workspace_path)
    if workspace_text not in workspaces:
        workspaces.append(workspace_text)
    write_private_json(path, data)
    _RUNTIME.configure_workspace(workspace_path)
    return path


def untrust_mcp_workspace(workspace: str | Path) -> Path:
    """Disable project MCP config for one workspace."""
    workspace_text = str(Path(workspace).expanduser().resolve())
    path = _trusted_mcp_projects_path(create=True)
    data = _read_json_file(path) or {"workspaces": []}
    data["workspaces"] = [item for item in data.get("workspaces", []) if item != workspace_text]
    write_private_json(path, data)
    _RUNTIME.configure_workspace(workspace_text)
    return path


def is_mcp_workspace_trusted(workspace: str | Path) -> bool:
    """Return whether project MCP config is trusted for workspace."""
    workspace_text = str(Path(workspace).expanduser().resolve())
    data = _read_json_file(_trusted_mcp_projects_path(create=False)) or {}
    return workspace_text in set(str(item) for item in data.get("workspaces", []))


def _load_server_configs(workspace: Path) -> Dict[str, MCPServerConfig]:
    config_path = workspace / ".mcp.json"
    data = _read_json_file(config_path)
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        return {}

    configs: Dict[str, MCPServerConfig] = {}
    for raw_name, raw_config in servers.items():
        if not isinstance(raw_config, dict):
            continue
        command = raw_config.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        name = _normalize_component(str(raw_name)) or f"server_{len(configs) + 1}"
        cwd = _resolve_server_cwd(workspace, raw_config.get("cwd"))
        args = raw_config.get("args", [])
        env = raw_config.get("env", {})
        configs[name] = MCPServerConfig(
            name=name,
            command=command,
            args=[str(item) for item in args] if isinstance(args, list) else [],
            env={str(key): str(value) for key, value in env.items()} if isinstance(env, dict) else {},
            cwd=cwd,
            disabled=bool(raw_config.get("disabled", False)),
        )
    return configs


def _resolve_server_cwd(workspace: Path, configured_cwd: Any) -> Optional[Path]:
    if not configured_cwd:
        return workspace
    candidate = Path(str(configured_cwd)).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError as exc:
        raise MCPRuntimeError(f"MCP cwd must stay inside workspace: {configured_cwd}") from exc
    return resolved


def _build_tool_info(server_name: str, raw_tool: Dict[str, Any]) -> MCPToolInfo:
    original_name = str(raw_tool.get("name") or "tool")
    server_part = _normalize_component(server_name)
    tool_part = _normalize_component(original_name)
    alias = f"mcp_{server_part}_{tool_part}"
    return MCPToolInfo(
        alias=alias,
        server_name=server_name,
        name=original_name,
        description=str(raw_tool.get("description") or f"MCP tool {server_name}.{original_name}"),
        input_schema=raw_tool.get("inputSchema") if isinstance(raw_tool.get("inputSchema"), dict) else {},
    )


def _build_langchain_tool(info: MCPToolInfo, caller: Any | None = None) -> StructuredTool:
    args_schema = _json_schema_to_model(info.alias, info.input_schema)
    tool_caller = caller or call_mcp_tool

    def remote_tool(**kwargs: Any) -> str:
        return tool_caller(info.alias, kwargs)

    remote_tool.__name__ = info.alias
    return StructuredTool.from_function(
        func=remote_tool,
        name=info.alias,
        description=f"[MCP:{info.server_name}] {info.description}",
        args_schema=args_schema,
    )


def _json_schema_to_model(alias: str, schema: Dict[str, Any]) -> type:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    fields: Dict[str, tuple[Any, Any]] = {}

    if isinstance(properties, dict):
        for raw_name, prop in properties.items():
            name = str(raw_name)
            if not name.isidentifier():
                continue
            prop_schema = prop if isinstance(prop, dict) else {}
            py_type = _json_type_to_python(prop_schema)
            description = str(prop_schema.get("description") or "")
            default = ... if name in required else None
            fields[name] = (py_type, Field(default, description=description))

    model_name = "MCPArgs_" + re.sub(r"[^A-Za-z0-9_]", "_", alias)
    return create_model(model_name, **fields)


def _json_type_to_python(schema: Dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "string")
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }.get(str(schema_type), Any)


def _format_tool_result(result: Dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        text = "\n".join(part for part in parts if part)
    else:
        text = json.dumps(result, ensure_ascii=False)

    if result.get("isError"):
        text = "❌ " + text
    return text[:MCP_MAX_OUTPUT] + ("...[truncated]" if len(text) > MCP_MAX_OUTPUT else "")


def _normalize_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_").lower()
    return normalized or "item"


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(tr("core.json_read_failed", error=str(e)))
        return {}
    return data if isinstance(data, dict) else {}


def _sayacode_home(create: bool = False) -> Path:
    path = SayacodePaths.resolve(create=False).home
    return ensure_private_dir(path) if create else path


def _trusted_mcp_projects_path(create: bool = False) -> Path:
    return _sayacode_home(create=create) / "mcp_trusted_projects.json"


def _popen_platform_kwargs() -> Dict[str, Any]:
    if sys.platform.startswith("win"):
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            # 静默忽略：taskkill 失败，回退到 process.kill()
            try:
                process.kill()
            except Exception:
                # 静默忽略：进程清理非关键路径
                pass
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except Exception:
        # 静默忽略：SIGTERM 等失败，回退到 process.kill()
        try:
            process.kill()
        except Exception:
            # 静默忽略：进程清理非关键路径
            pass


_RUNTIME = MCPRuntime()
atexit.register(_RUNTIME.shutdown)


__all__ = [
    "MCPRuntime",
    "MCPRuntimeError",
    "MCPServerConfig",
    "MCPToolInfo",
    "call_mcp_tool",
    "configure_mcp_workspace",
    "get_mcp_status",
    "is_mcp_workspace_trusted",
    "load_mcp_tools",
    "reload_mcp_tools",
    "shutdown_mcp_runtime",
    "trust_mcp_workspace",
    "untrust_mcp_workspace",
]
