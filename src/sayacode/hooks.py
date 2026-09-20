"""Opt-in command hooks for the six public agent lifecycle events."""

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

from .tools import (
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
    event: str
    command: str | tuple[str, ...]
    source: str
    name: str
    blocking: bool = False
    timeout: float = 10.0


@dataclass(frozen=True, slots=True)
class HookResult:
    event: str
    name: str
    source: str
    returncode: int
    stdout: str
    stderr: str
    blocked: bool


class HookRuntime:
    """Run configured hooks; project commands require explicit workspace trust."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        state_home: str | Path | None = None,
        audit: Callable[[HookResult], Any] | None = None,
        trust_origin: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.trust_origin = Path(trust_origin).expanduser().resolve() if trust_origin else self.workspace
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
        return self.state_home / "trusted_hook_projects.json"

    def _trusted_paths(self) -> set[str]:
        try:
            value = json.loads(self.trust_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        entries = value.get("projects", []) if isinstance(value, dict) else value
        return set(entries) if isinstance(entries, list) else set()

    @property
    def project_trusted(self) -> bool:
        return str(self.trust_origin) in self._trusted_paths()

    def _write_trust(self, projects: set[str]) -> None:
        self.state_home.mkdir(parents=True, exist_ok=True)
        temporary = self.trust_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"projects": sorted(projects)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.trust_path)

    def trust(self) -> None:
        projects = self._trusted_paths()
        projects.add(str(self.workspace))
        self._write_trust(projects)
        self.reload()

    def untrust(self) -> None:
        projects = self._trusted_paths()
        projects.discard(str(self.workspace))
        self._write_trust(projects)
        self.reload()

    def reload(self) -> None:
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
        return {
            "workspace": str(self.workspace),
            "project_trusted": self.project_trusted,
            "user_hooks": sum(spec.source == "user" for spec in self.hooks),
            "project_hooks": sum(spec.source == "project" for spec in self.hooks),
            "warnings": list(self.warnings),
        }

    async def trigger(self, event: str, payload: dict[str, Any] | None = None) -> str | None:
        """Return the first blocking reason, or None if the event may proceed."""
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
        try:
            return await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            return b"", b"Timed-out hook process did not close its output pipes"


class HookMiddleware(AgentMiddleware):
    """Emit exactly one Hook lifecycle sequence around each graph tool call."""

    def __init__(self, hooks: HookRuntime) -> None:
        super().__init__()
        self.hooks = hooks

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
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
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content[:8000]
    return str(content)[:8000]


__all__ = ["HOOK_EVENTS", "HookMiddleware", "HookResult", "HookRuntime", "HookSpec"]
