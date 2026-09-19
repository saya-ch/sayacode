"""最小化的 stdio MCP 运行时。

本模块实现 SAYACODE 使用本地 MCP 工具所需的子集：
进程生命周期、initialize、tools/list，以及基于 JSON-RPC stdio 的 tools/call。
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
    """一个已配置的 stdio MCP server。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[Path] = None
    disabled: bool = False


@dataclass(frozen=True)
class MCPToolInfo:
    """一个已发现的 MCP 工具。"""

    alias: str
    server_name: str
    name: str
    description: str
    input_schema: Dict[str, Any]


class MCPRuntimeError(RuntimeError):
    """MCP 运行时失败。"""


class MCPServerClient:
    """面向单个 MCP server 的同步 JSON-RPC stdio 客户端。"""

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
        """返回 server 进程是否存活。"""
        return bool(self.process and self.process.poll() is None)

    def start(self) -> None:
        """启动 MCP server 并完成初始化。"""
        if self.config.disabled:
            raise MCPRuntimeError("server is disabled")
        if self.active:
            return

        command = [self.config.command, *self.config.args]
        cwd = self.config.cwd or self.workspace
        env = build_process_env()
        env.update({key: str(value) for key, value in self.config.env.items() if not _is_forbidden_mcp_env_key(key)})
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
        """停止 MCP server 进程。"""
        process = self.process
        if not process or process.poll() is not None:
            return
        _terminate_process_tree(process)

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用远端 MCP 工具并返回文本。"""
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=MCP_REQUEST_TIMEOUT,
        )
        return _format_tool_result(result)

    def status(self) -> Dict[str, Any]:
        """返回单个 server 的运行状态。"""
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
    """工作区级 MCP 进程与工具注册表。

    调用前安全复检直接用底层共享规则，和图内安全中间件同一套。
    spill_workspace 可选传入超长结果落盘目录，缺省用本运行时工作区，
    再没有则用进程 cwd。
    """

    def __init__(
        self,
        permissions: Optional[Any] = None,
        hooks: Optional[Any] = None,
        *,
        spill_workspace: Optional[str | Path] = None,
    ) -> None:
        self.workspace: Optional[Path] = None
        self.config_path: Optional[Path] = None
        self.configured_servers: Dict[str, MCPServerConfig] = {}
        self.clients: Dict[str, MCPServerClient] = {}
        self.tools_by_alias: Dict[str, MCPToolInfo] = {}
        self.errors: Dict[str, str] = {}
        self.project_trusted = False
        self.permissions = permissions
        self.hooks = hooks
        self.spill_workspace = Path(spill_workspace).expanduser() if spill_workspace else None

    def _spill_workspace(self) -> Path:
        """超长结果落盘目录：注入 > 本运行时工作区 > 进程 cwd。"""
        if self.spill_workspace is not None:
            return self.spill_workspace
        if self.workspace is not None:
            try:
                return Path(self.workspace)
            except Exception:
                pass
        return Path.cwd()

    def _check_args_safety(self, flat: Dict[str, Any]) -> str:
        """对扁平实参做安全否决；通过返回空串，否则返回阻断消息。

        判据直接用底层共享规则，和图内安全中间件同一套。
        """
        from .safety_rules import check_command_danger, check_file_danger, find_safety_target

        try:
            target = find_safety_target(flat, extra_file_keys=("cwd",))
        except Exception:
            return ""
        if target is None:
            return ""
        kind, value = target
        try:
            if kind == "command":
                safe, reason = check_command_danger(value)
            else:
                safe, reason = check_file_danger(value)
        except Exception:
            return "⚠️ 安全检查失败：MCP 安全判定异常，已拦截"
        if not safe:
            return f"⚠️ 安全检查失败：{reason}"
        return ""

    def configure_workspace(self, workspace: str | Path) -> None:
        """加载指定工作区的 MCP 配置。"""
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
        """启动受信 server 并返回工具列表。"""
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
                    tools.append(_build_langchain_tool(
                        info,
                        caller=self.call_tool,
                        spill_workspace=self._spill_workspace(),
                    ))
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
        """按别名调用已注册的 MCP 工具。"""
        with ExitStack() as stack:
            if self.permissions is not None:
                stack.enter_context(permission_runtime_session(self.permissions))
            if self.hooks is not None:
                stack.enter_context(hook_runtime_session(self.hooks))
            return self._call_tool(alias, arguments)

    def _call_tool(self, alias: str, arguments: Dict[str, Any], emit_events: bool = True) -> str:
        info = self.tools_by_alias.get(alias)
        if not info:
            if emit_events:
                append_audit_event("mcp", alias, workspace=self.workspace, allowed=False, details={"error": "not_registered"})
            return f"❌ MCP tool is not registered: {alias}"

        flat = dict(arguments or {})
        nested = flat.get("arguments")
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
        safety_error = self._check_args_safety(flat)
        if safety_error:
            if emit_events:
                append_audit_event("mcp", alias, workspace=self.workspace, allowed=False, details={"reason": safety_error})
            return safety_error

        if emit_events:
            block_reason = trigger_hook_event(
                "PreToolUse",
                {"tool_name": alias, "arguments": arguments, "mcp_server": info.server_name},
            )
        else:
            block_reason = ""
        if block_reason:
            if emit_events:
                append_audit_event("mcp", alias, workspace=self.workspace, allowed=False, details={"reason": block_reason})
            return f"⚠️ {block_reason}"

        perm_args: Dict[str, Any] = {"server": info.server_name, "tool": info.name, "arguments": arguments}
        perm_args.update(flat)
        permission_error = enforce_tool_permission(alias, perm_args)
        if permission_error:
            if emit_events:
                append_audit_event("mcp", alias, workspace=self.workspace, allowed=False, details={"reason": permission_error})
            return permission_error

        client = self.clients.get(info.server_name)
        if not client:
            if emit_events:
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
            if emit_events:
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

        if emit_events:
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
        """返回全局 MCP 运行状态。"""
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
        """停止全部 MCP server 进程。"""
        for client in list(self.clients.values()):
            client.shutdown()
        self.clients.clear()


def configure_mcp_workspace(workspace: str | Path) -> None:
    """为工作区配置全局 MCP 运行时。"""
    _RUNTIME.configure_workspace(workspace)


def load_mcp_tools(server_names: Optional[list[str]] = None) -> list[StructuredTool]:
    """启动受信任的 MCP server 并返回 LangChain 工具。"""
    return _RUNTIME.load_tools(server_names=server_names)


def reload_mcp_tools(server_names: Optional[list[str]] = None) -> list[StructuredTool]:
    """重启 MCP server 并重新发现工具。"""
    return _RUNTIME.load_tools(server_names=server_names)


def call_mcp_tool(alias: str, arguments: Optional[Dict[str, Any]] = None) -> str:
    """按 SAYACODE alias 调用一个已注册的 MCP 工具。"""
    return _RUNTIME.call_tool(alias, arguments or {})


def get_mcp_status() -> Dict[str, Any]:
    """返回全局 MCP 运行时状态。"""
    return _RUNTIME.status()


def shutdown_mcp_runtime() -> None:
    """停止所有 MCP server 进程。"""
    _RUNTIME.shutdown()


def trust_mcp_workspace(workspace: str | Path) -> Path:
    """信任某个工作区的 project MCP 配置。"""
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
    """禁用某个工作区的 project MCP 配置。"""
    workspace_text = str(Path(workspace).expanduser().resolve())
    path = _trusted_mcp_projects_path(create=True)
    data = _read_json_file(path) or {"workspaces": []}
    data["workspaces"] = [item for item in data.get("workspaces", []) if item != workspace_text]
    write_private_json(path, data)
    _RUNTIME.configure_workspace(workspace_text)
    return path


def is_mcp_workspace_trusted(workspace: str | Path) -> bool:
    """返回该工作区的 project MCP 配置是否已被信任。"""
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
    input_schema = raw_tool.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = {}
    return MCPToolInfo(
        alias=alias,
        server_name=server_name,
        name=original_name,
        description=str(raw_tool.get("description") or f"MCP tool {server_name}.{original_name}"),
        input_schema=input_schema,
    )


def _build_langchain_tool(
    info: MCPToolInfo,
    caller: Any | None = None,
    spill_workspace: Path | None = None,
) -> StructuredTool:
    """组装 LangChain 工具：大输出走 _spill_oversized_result 统一落盘。"""
    args_schema = _json_schema_to_model(info.alias, info.input_schema)
    tool_caller = caller or call_mcp_tool

    def remote_tool(**kwargs: Any):
        raw = tool_caller(info.alias, kwargs)
        content, artifact = _spill_oversized_result(str(raw or ""), info.alias, workspace=spill_workspace)
        return content, artifact

    remote_tool.__name__ = info.alias
    return StructuredTool.from_function(
        func=remote_tool,
        name=info.alias,
        description=f"[MCP:{info.server_name}] {info.description}",
        args_schema=args_schema,
        response_format="content_and_artifact",
    )


def _spill_oversized_result(
    text: str,
    tool_alias: str,
    workspace: Path | None = None,
) -> tuple[str, Dict[str, Any]]:
    """超长结果落盘并返回预览与 artifact；小结果直接透传。

    落盘目录由调用方传入；没传时回落到文件工具默认工作区，
    再没有则用进程 cwd。回落用函数内惰性导入，不形成模块循环。
    """
    content = str(text or "")
    if len(content) <= MCP_MAX_OUTPUT:
        return content, {}
    try:
        if workspace is not None:
            spill_dir = Path(workspace)
        else:
            try:
                from ..tools.file_tools import get_default_workspace

                spill_dir = Path(get_default_workspace())
            except Exception:
                spill_dir = Path.cwd()
        from .spill import preview_with_locator, spill_text
        from .tool_result import build_tool_artifact

        path = spill_text(spill_dir, tool_alias, content, suggested_name=tool_alias)
        if path is None:
            raise OSError("spill failed")
        preview = preview_with_locator(content, path, MCP_MAX_OUTPUT)
        artifact = build_tool_artifact(
            tool_alias, "spilled", chars=len(content), spill_path=str(path), truncated=True
        )
        return preview, artifact
    except Exception:
        # 落盘失败退回截断：artifact 保持裸 {"truncated": True} 旧契约
        #（测试按精确相等断言，消费方按此形状识别降级路径）。
        truncated = content[:MCP_MAX_OUTPUT] + "...[truncated]"
        return truncated, {"truncated": True}


def _json_schema_to_model(alias: str, schema: Dict[str, Any]) -> type:
    """JSONSchema 转 pydantic 模型（仅建模，不做校验语义外延）。

    类型映射归 _json_type_to_python 唯一入口；非法字段名直接跳过
    （与 LangChain 工具命名约束一致），删除 required/默认分支重复。
    """
    if not isinstance(schema, dict):
        schema = {}
    properties = schema.get("properties", {})
    required = set(schema.get("required", []) or [])
    if not isinstance(properties, dict):
        properties = {}
    fields: Dict[str, tuple[Any, Any]] = {}

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
    return text


def _normalize_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_").lower()
    return normalized or "item"


def _is_forbidden_mcp_env_key(key: str) -> bool:
    """MCP 配置 env 禁止覆盖的变量（防注入）。"""
    upper = str(key or "").upper()
    return upper.startswith("LD_") or upper.startswith("PYTHON") or upper in {"NODE_OPTIONS", "PATH"}


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
