"""外部命令钩子，在智能体生命周期关键点触发外部进程。

项目级配置默认不可信，需显式信任后才加载。用户级配置始终加载。
触发时按配置顺序依次执行，阻塞型钩子可中断当前流程。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from ..process import (
    attach_process_tree,
    close_process_tree,
    process_creation_options,
    stop_process_tree,
)

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "ToolFailure",
    "SessionEnd",
)


@dataclass(frozen=True, slots=True)
class HookSpec:
    """单条钩子配置，描述触发事件与执行方式。

    事件名限定为公开事件集合。命令可为字符串或参数元组。
    来源区分用户级与项目级。阻塞标记决定失败时是否中断流程。
    超时以秒为单位，加载时会被收敛到合理区间。"""
    event: str
    command: str | tuple[str, ...]
    source: str
    name: str
    blocking: bool = False
    timeout: float = 10.0


@dataclass(frozen=True, slots=True)
class HookResult:
    """单次钩子执行结果，记录输出与是否拦截。

    事件名与名称用于定位来源。返回码与标准输出错误均为截断后文本。
    拦截标记已综合阻塞配置与执行结果，调用方可直接判断。"""
    event: str
    name: str
    source: str
    returncode: int
    stdout: str
    stderr: str
    blocked: bool


class HookRuntime:
    """钩子运行器，负责加载配置与顺序触发。

    构造时即加载一次配置。用户级配置始终生效。
    项目级配置仅在受信后生效，未受信时只记录告警。
    触发顺序与配置列表顺序一致，阻塞钩子先返回即停止后继判断。
    执行记录只保留最近一段，避免无界增长。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        state_home: str | Path | None = None,
        audit: Callable[[HookResult], Any] | None = None,
        trust_origin: str | Path | None = None,
    ) -> None:
        """创建运行器并完成首次加载。

        参数为工作区路径与可选状态目录。审计回调用于记录每次结果。
        信任起点默认等于工作区，多工作区共享时可另行指定。
        约束是构造即调用加载，调用前无需手动初始化。
        坑点是工作区与状态目录都会被解析为绝对路径。"""
        self.workspace = Path(workspace).expanduser().resolve()
        self.trust_origin = (
            Path(trust_origin).expanduser().resolve() if trust_origin else self.workspace
        )
        default_home = os.environ.get("SAYACODE_HOME")
        self.state_home = (
            Path(state_home or default_home or Path.home() / ".sayacode").expanduser().resolve()
        )
        self.audit = audit
        self.results: list[HookResult] = []
        self.warnings: list[str] = []
        self.hooks: list[HookSpec] = []
        self._loaded_project_trust = False
        self.reload()

    @property
    def trust_path(self) -> Path:
        """返回信任名单文件路径。

        无参数输入。返回状态目录下的固定文件名。
        调用约束是目录可能尚不存在，读取时需容忍缺失。"""
        return self.state_home / "trusted_hook_projects.json"

    def _trusted_paths(self) -> set[str]:
        """读取信任名单，文件缺失或损坏时返回空集合。"""
        try:
            value = json.loads(self.trust_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        entries = value.get("projects", []) if isinstance(value, dict) else value
        return set(entries) if isinstance(entries, list) else set()

    @property
    def project_trusted(self) -> bool:
        """判断当前信任起点是否在名单中。

        无参数输入。返回是否受信的布尔值。
        每次调用都会重新读文件，信任变更即时可见。
        坑点是比较的是信任起点而非工作区本身。"""
        return str(self.trust_origin) in self._trusted_paths()

    def _write_trust(self, projects: set[str]) -> None:
        """先写临时文件再替换，原子更新信任名单。"""
        self.state_home.mkdir(parents=True, exist_ok=True)
        temporary = self.trust_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"projects": sorted(projects)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.trust_path)

    def trust(self) -> None:
        """将当前工作区加入信任名单并重载。

        无参数输入，无返回值。
        调用后项目级钩子立即生效。
        坑点是写入的是工作区而非信任起点。"""
        projects = self._trusted_paths()
        projects.add(str(self.workspace))
        self._write_trust(projects)
        self.reload()

    def untrust(self) -> None:
        """将当前工作区移出信任名单并重载。

        无参数输入，无返回值。
        调用后项目级钩子不再加载，已加载的即时清空。
        与受信操作对称，同样会触发一次重载。"""
        projects = self._trusted_paths()
        projects.discard(str(self.workspace))
        self._write_trust(projects)
        self.reload()

    def reload(self) -> None:
        """清空后重载用户级与项目级配置。

        无参数输入，无返回值。
        先装载用户级，再按受信状态决定是否装载项目级。
        未受信的项目配置会转为告警而非直接丢弃。
        坑点是会重置告警列表与受信快照。"""
        self.hooks = []
        self.warnings = []
        self._loaded_project_trust = self.project_trusted
        self.hooks.extend(self._load(self.state_home / "hooks.json", "user"))
        project_path = self.workspace / ".sayacode" / "hooks.json"
        if project_path.is_file():
            if self._loaded_project_trust:
                self.hooks.extend(self._load(project_path, "project"))
            else:
                self.warnings.append(f"Project hooks are untrusted: {project_path}")

    def _load(self, path: Path, source: str) -> list[HookSpec]:
        """解析单个配置文件，非法条目只跳过并告警。

        分三步处理。先读文件并容忍缺失，损坏则记告警返回空。
        再兼容两种外层形状，取钩子映射表，非字典视为非法。
        最后逐事件逐条校验，未知事件与非命令类型直接跳过。
        命令只接受非空字符串或全为非空字符串的列表。
        超时会收敛到区间内，非法超时回落为默认值。
        阻塞默认随事件而定，显式配置优先。"""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            self.warnings.append(f"Could not load {path}: {exc}")
            return []
        mapping = document.get("hooks", document) if isinstance(document, dict) else {}
        if not isinstance(mapping, dict):
            self.warnings.append(f"Invalid hooks config: {path}")
            return []
        found: list[HookSpec] = []
        for event, entries in mapping.items():
            if event not in HOOK_EVENTS or not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                items = entry.get("hooks", [entry]) if isinstance(entry, dict) else []
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict) or item.get("type", "command") != "command":
                        continue
                    raw = item.get("command")
                    if isinstance(raw, str) and raw.strip():
                        command: str | tuple[str, ...] = raw
                    elif (
                        isinstance(raw, list)
                        and raw
                        and all(isinstance(part, str) and part for part in raw)
                    ):
                        command = tuple(raw)
                    else:
                        continue
                    timeout = item.get("timeout", 10)
                    try:
                        bounded_timeout = max(0.1, min(float(timeout), 30.0))
                    except (TypeError, ValueError):
                        bounded_timeout = 10.0
                    found.append(
                        HookSpec(
                            event=event,
                            command=command,
                            source=source,
                            name=str(item.get("name") or f"{source}:{event}:{index + 1}"),
                            blocking=bool(
                                item.get("blocking", event in {"UserPromptSubmit", "PreToolUse"})
                            ),
                            timeout=bounded_timeout,
                        )
                    )
        return found

    def status(self) -> dict[str, Any]:
        """返回工作区信任状态与钩子数量快照。

        无参数输入。返回含路径与计数及告警的字典。
        只读操作，不触发重载。
        计数按来源分别统计，便于排查配置来源。"""
        return {
            "workspace": str(self.workspace),
            "project_trusted": self.project_trusted,
            "user_hooks": sum(spec.source == "user" for spec in self.hooks),
            "project_hooks": sum(spec.source == "project" for spec in self.hooks),
            "warnings": list(self.warnings),
        }

    async def trigger(self, event: str, payload: dict[str, Any] | None = None) -> str | None:
        """按配置顺序触发同事件钩子，返回首个阻塞原因。

        参数为事件名与附加负载。返回阻塞文案，无阻塞则返回空。
        分四步执行。先校验事件合法性，再按需重载以跟进信任变更。
        然后构造统一输入并依次执行，最后记录结果并回调审计。
        约束是未知事件直接抛错，审计回调可为同步或异步。
        坑点是结果列表只保留最近一段，历史会被滚动丢弃。"""
        if event not in HOOK_EVENTS:
            raise ValueError(f"Unknown hook event: {event}")
        if self.project_trusted != self._loaded_project_trust:
            self.reload()
        body = json.dumps(
            {"event": event, "workspace": str(self.workspace), "payload": payload or {}},
            ensure_ascii=False,
            default=str,
        ).encode()
        for spec in self.hooks:
            if spec.event != event:
                continue
            result = await self._execute(spec, body)
            self.results.append(result)
            self.results = self.results[-200:]
            if self.audit is not None:
                recorded = self.audit(result)
                if inspect.isawaitable(recorded):
                    await recorded
            if result.blocked:
                reason = result.stderr or result.stdout or f"exit {result.returncode}"
                return f"Hook {result.name} blocked {event}: {reason[:500]}"
        return None

    async def _execute(self, spec: HookSpec, body: bytes) -> HookResult:
        """执行单条钩子进程并判定是否拦截。

        分四步处理。先按字符串或参数表选择创建方式，工作目录固定为工作区。
        再接入进程树并等待输出，超时则终止整树并标记专用返回码。
        然后截断输出并解析拦截意图，非标准输出视为普通文本。
        最后综合阻塞配置与返回码生成结果。
        进程启动失败会转为固定错误码，不向外抛错。
        取消会先清理进程树再继续向上传播。"""
        try:
            process_options = process_creation_options()
            if isinstance(spec.command, str):
                process = await asyncio.create_subprocess_shell(
                    spec.command,
                    cwd=str(self.workspace),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **process_options,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *spec.command,
                    cwd=str(self.workspace),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **process_options,
                )
            job = attach_process_tree(process)
            try:
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(body), timeout=spec.timeout
                    )
                    returncode = int(process.returncode or 0)
                except asyncio.TimeoutError:
                    await stop_process_tree(process, job)
                    stdout, stderr = await self._bounded_communicate(process)
                    returncode = 124
                except asyncio.CancelledError:
                    await stop_process_tree(process, job)
                    await self._bounded_communicate(process)
                    raise
            finally:
                close_process_tree(job)
        except OSError as exc:
            stdout, stderr, returncode = b"", str(exc).encode(), 127
        out = stdout.decode("utf-8", errors="replace")[:8000]
        err = stderr.decode("utf-8", errors="replace")[:8000]
        requested_block = False
        try:
            response = json.loads(out)
            requested_block = isinstance(response, dict) and response.get("decision") == "block"
        except ValueError:
            pass
        return HookResult(
            spec.event,
            spec.name,
            spec.source,
            returncode,
            out,
            err,
            spec.blocking and (returncode != 0 or requested_block),
        )

    @staticmethod
    async def _bounded_communicate(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """限时回收超时进程残留输出，超时则返回提示文本。"""
        try:
            return await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            return b"", b"Timed-out hook process did not close its output pipes"


class HookMiddleware(AgentMiddleware):
    """工具调用中间件，在调用前后触发对应钩子序列。

    前置钩子可拦截执行，后置钩子只做通知。
    失败钩子覆盖异常与错误结果两种路径。
    触发顺序固定为前置优先，失败与成功互斥。"""

    def __init__(self, hooks: HookRuntime) -> None:
        super().__init__()
        self.hooks = hooks

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        """包装单次工具调用，依次触发前置失败后置钩子。

        参数为调用请求与后继处理器。返回处理器结果或拦截消息。
        分三步执行。先触发前置钩子，被拦截则直接返回错误消息。
        再执行真实工具，异常时触发失败钩子并原样抛错。
        最后按结果状态二选一触发失败或后置钩子。
        约束是拦截不会执行真实工具，失败钩子本身不改变返回。
        坑点是参数缺失时会回落为默认值，不会抛错。"""
        call = request.tool_call or {}
        name = str(call.get("name") or "tool")
        arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
        blocked = await self.hooks.trigger(
            "PreToolUse", {"tool_name": name, "arguments": arguments}
        )
        if blocked:
            return ToolMessage(
                content=blocked,
                status="error",
                name=name,
                tool_call_id=call.get("id"),
                artifact={"action": "hook_block"},
            )
        try:
            result = await handler(request)
        except Exception as exc:
            await self.hooks.trigger(
                "ToolFailure",
                {"tool_name": name, "arguments": arguments, "error": str(exc)},
            )
            raise
        if isinstance(result, ToolMessage) and result.status == "error":
            await self.hooks.trigger(
                "ToolFailure",
                {"tool_name": name, "arguments": arguments, "error": _message_content(result)},
            )
        else:
            await self.hooks.trigger(
                "PostToolUse",
                {"tool_name": name, "arguments": arguments, "result": _message_content(result)},
            )
        return result


def _message_content(value: Any) -> str:
    """提取工具结果文本并截断，便于钩子负载传递。"""
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content[:8000]
    return str(content)[:8000]


__all__ = ["HOOK_EVENTS", "HookMiddleware", "HookResult", "HookRuntime", "HookSpec"]
