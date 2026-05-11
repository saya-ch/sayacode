"""Lifecycle hook runtime for SAYACODE."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Literal, Optional
import json
import subprocess

from .audit import append_audit_event
from ..i18n import tr
from .paths import SayacodePaths
from .private_io import ensure_private_dir, write_private_json
from .process_env import build_process_env


HookEventName = Literal[
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "ToolFailure",
    "SessionEnd",
]

HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "ToolFailure",
    "SessionEnd",
)

DEFAULT_HOOK_TIMEOUT = 10
MAX_HOOK_TIMEOUT = 30
MAX_HOOK_OUTPUT = 8000
MAX_HOOK_FIELD = 2000


@dataclass(frozen=True)
class HookCommand:
    """One configured command hook."""

    event: str
    command: str | list[str]
    source: str
    name: str
    blocking: bool
    timeout: int


@dataclass(frozen=True)
class HookRunResult:
    """Result of one hook command execution."""

    event: str
    name: str
    source: str
    returncode: int
    stdout: str
    stderr: str
    blocked: bool


class HookRuntime:
    """Process-wide hook policy and execution state."""

    def __init__(self) -> None:
        self.workspace: Optional[Path] = None
        self.user_hooks: list[HookCommand] = []
        self.project_hooks: list[HookCommand] = []
        self.project_hooks_trusted = False
        self.warnings: list[str] = []
        self.audit_log: list[Dict[str, Any]] = []
        self.configure_workspace(Path.cwd())

    def configure_workspace(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        paths = SayacodePaths.resolve(create=False)
        self.user_hooks = _load_hooks_file(paths.user_hooks, source="user")
        project_path = paths.project_hooks(self.workspace)
        self.project_hooks_trusted = is_hook_workspace_trusted(self.workspace)
        self.project_hooks = []
        self.warnings = []

        if project_path.exists():
            if self.project_hooks_trusted:
                self.project_hooks = _load_hooks_file(project_path, source="project")
            else:
                self.warnings.append(
                    f"Project hooks exist but are not trusted: {project_path}"
                )

    def trigger(self, event: str, payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
        normalized_event = _normalize_event(event)
        if not normalized_event:
            return None

        hooks = [
            hook
            for hook in [*self.user_hooks, *self.project_hooks]
            if hook.event == normalized_event
        ]
        if not hooks:
            return None

        event_payload = _build_event_payload(
            event=normalized_event,
            workspace=self.workspace,
            payload=payload or {},
        )

        for hook in hooks:
            result = _run_command_hook(hook, event_payload, cwd=self.workspace)
            self._record(result)
            if result.blocked:
                detail = result.stderr or result.stdout or f"exit code {result.returncode}"
                return f"Hook '{result.name}' blocked {normalized_event}: {detail}"
        return None

    def status(self) -> Dict[str, Any]:
        return {
            "workspace": str(self.workspace) if self.workspace else "",
            "user_hooks": len(self.user_hooks),
            "project_hooks": len(self.project_hooks),
            "project_hooks_trusted": self.project_hooks_trusted,
            "warnings": list(self.warnings),
        }

    def _record(self, result: HookRunResult) -> None:
        self.audit_log.append({
            "event": result.event,
            "name": result.name,
            "source": result.source,
            "returncode": result.returncode,
            "blocked": result.blocked,
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:500],
        })
        if len(self.audit_log) > 200:
            self.audit_log = self.audit_log[-100:]
        append_audit_event(
            "hook",
            result.event,
            workspace=self.workspace,
            allowed=not result.blocked,
            details={
                "name": result.name,
                "source": result.source,
                "returncode": result.returncode,
                "blocked": result.blocked,
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:500],
            },
        )


def configure_hooks_workspace(workspace: str | Path) -> None:
    """Reload hook configuration for a workspace."""
    _active_runtime().configure_workspace(workspace)


def get_hooks_workspace() -> Optional[Path]:
    """Return the active hook workspace."""
    return _active_runtime().workspace


def restore_hooks_workspace(workspace: Optional[str | Path]) -> None:
    """Restore hook runtime to a previous workspace."""
    runtime = _active_runtime()
    if workspace is None:
        runtime.workspace = None
        runtime.user_hooks = []
        runtime.project_hooks = []
        runtime.project_hooks_trusted = False
        runtime.warnings = []
        return
    runtime.configure_workspace(workspace)


def trigger_hook_event(event: str, payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Run hooks for an event. Returns a blocking reason when blocked."""
    return _active_runtime().trigger(event, payload)


def get_hook_status() -> Dict[str, Any]:
    """Return current hook runtime status."""
    return _active_runtime().status()


def render_hook_status() -> str:
    """Render hook status for CLI display."""
    status = get_hook_status()
    lines = [
        tr("hooks.status_title"),
        tr("hooks.status_workspace", workspace=status["workspace"]),
        tr("hooks.status_user", count=status["user_hooks"]),
        tr("hooks.status_project", count=status["project_hooks"]),
        tr("hooks.status_trusted", value=status["project_hooks_trusted"]),
    ]
    warnings = status.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append(tr("hooks.status_warnings"))
        lines.extend(f"  {warning}" for warning in warnings)
    return "\n".join(lines)


def get_hook_audit_log() -> list[Dict[str, Any]]:
    """Return recent hook executions."""
    return list(_active_runtime().audit_log)


def trust_hook_workspace(workspace: str | Path) -> Path:
    """Trust project hooks for one workspace."""
    workspace_path = Path(workspace).expanduser().resolve()
    path = _trusted_projects_path(create=True)
    data = _read_json_file(path) or {"workspaces": []}
    workspaces = data.setdefault("workspaces", [])
    workspace_text = str(workspace_path)
    if workspace_text not in workspaces:
        workspaces.append(workspace_text)
    write_private_json(path, data)
    _active_runtime().configure_workspace(workspace_path)
    return path


def untrust_hook_workspace(workspace: str | Path) -> Path:
    """Remove project hook trust for one workspace."""
    workspace_text = str(Path(workspace).expanduser().resolve())
    path = _trusted_projects_path(create=True)
    data = _read_json_file(path) or {"workspaces": []}
    data["workspaces"] = [item for item in data.get("workspaces", []) if item != workspace_text]
    write_private_json(path, data)
    _active_runtime().configure_workspace(workspace_text)
    return path


def is_hook_workspace_trusted(workspace: str | Path) -> bool:
    """Return whether project hooks are trusted for workspace."""
    workspace_text = str(Path(workspace).expanduser().resolve())
    data = _read_json_file(_trusted_projects_path(create=False)) or {}
    return workspace_text in set(str(item) for item in data.get("workspaces", []))


def sanitize_hook_payload(value: Any, key: str = "") -> Any:
    """Create a bounded, redacted payload safe for local hook stdin."""
    if _is_sensitive_key(key):
        return "***"

    if isinstance(value, dict):
        return {
            str(item_key): sanitize_hook_payload(item_value, str(item_key))
            for item_key, item_value in list(value.items())[:50]
        }

    if isinstance(value, (list, tuple)):
        return [sanitize_hook_payload(item, key) for item in list(value)[:50]]

    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value) if value is not None else None
        if isinstance(value, str) and len(value) > MAX_HOOK_FIELD:
            return value[:MAX_HOOK_FIELD] + "...[truncated]"
        return value if text is None or len(text) <= MAX_HOOK_FIELD else text[:MAX_HOOK_FIELD]

    text = str(value)
    return text[:MAX_HOOK_FIELD] + ("...[truncated]" if len(text) > MAX_HOOK_FIELD else "")


def _run_command_hook(
    hook: HookCommand,
    event_payload: Dict[str, Any],
    cwd: Optional[Path],
) -> HookRunResult:
    env = build_process_env()
    env.update({
        "SAYACODE_HOOK_EVENT": hook.event,
        "SAYACODE_WORKSPACE": str(cwd or Path.cwd()),
        "GIT_TERMINAL_PROMPT": "0",
        "PIP_NO_INPUT": "1",
    })
    stdin_payload = json.dumps(event_payload, ensure_ascii=False)

    try:
        use_shell = isinstance(hook.command, str)
        result = subprocess.run(
            hook.command,
            cwd=str(cwd or Path.cwd()),
            env=env,
            input=stdin_payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=hook.timeout,
            shell=use_shell,
        )
        stdout = _truncate_output(result.stdout or "")
        stderr = _truncate_output(result.stderr or "")
        returncode = int(result.returncode or 0)
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = f"hook timed out after {hook.timeout}s"
        returncode = 124
    except Exception as exc:
        stdout = ""
        stderr = f"hook failed to run: {exc}"
        returncode = 1

    blocked = hook.blocking and returncode != 0
    return HookRunResult(
        event=hook.event,
        name=hook.name,
        source=hook.source,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        blocked=blocked,
    )


def _build_event_payload(
    event: str,
    workspace: Optional[Path],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "event": event,
        "workspace": str(workspace or Path.cwd()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": sanitize_hook_payload(payload),
    }


def _load_hooks_file(path: Path, source: str) -> list[HookCommand]:
    data = _read_json_file(path)
    if not data:
        return []

    hooks_node = data.get("hooks", data)
    if not isinstance(hooks_node, dict):
        return []

    hooks: list[HookCommand] = []
    for event, entries in hooks_node.items():
        normalized_event = _normalize_event(str(event))
        if not normalized_event:
            continue
        if not isinstance(entries, list):
            entries = [entries]
        for index, entry in enumerate(entries):
            command = _extract_hook_command(entry)
            if not command:
                continue
            hooks.append(
                HookCommand(
                    event=normalized_event,
                    command=command,
                    source=source,
                    name=_extract_hook_name(entry, normalized_event, index),
                    blocking=_extract_hook_blocking(entry, normalized_event),
                    timeout=_extract_hook_timeout(entry),
                )
            )
    return hooks


def _extract_hook_command(entry: Any) -> str | list[str] | None:
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if isinstance(command, str) and command.strip():
        return command
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return command
    return None


def _extract_hook_name(entry: Any, event: str, index: int) -> str:
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return f"{event}-{index + 1}"


def _extract_hook_blocking(entry: Any, event: str) -> bool:
    if isinstance(entry, dict) and "blocking" in entry:
        return bool(entry.get("blocking"))
    return event in {"UserPromptSubmit", "PreToolUse"}


def _extract_hook_timeout(entry: Any) -> int:
    value = entry.get("timeout", DEFAULT_HOOK_TIMEOUT) if isinstance(entry, dict) else DEFAULT_HOOK_TIMEOUT
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_HOOK_TIMEOUT
    return max(1, min(timeout, MAX_HOOK_TIMEOUT))


def _normalize_event(event: str) -> Optional[str]:
    normalized = event.strip().lower().replace("_", "").replace("-", "")
    for candidate in HOOK_EVENTS:
        if normalized == candidate.lower():
            return candidate
    return None


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _sayacode_home(create: bool = False) -> Path:
    path = SayacodePaths.resolve(create=False).home
    return ensure_private_dir(path) if create else path


def _trusted_projects_path(create: bool = False) -> Path:
    return _sayacode_home(create=create) / "trusted_projects.json"


def _truncate_output(output: str) -> str:
    if len(output) <= MAX_HOOK_OUTPUT:
        return output
    omitted = len(output) - MAX_HOOK_OUTPUT
    return output[:MAX_HOOK_OUTPUT] + f"\n...[hook output truncated, {omitted} chars omitted]"


def _is_sensitive_key(key: str) -> bool:
    normalized = key.upper()
    return any(marker in normalized for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"))


_RUNTIME = HookRuntime()
_RUNTIME_CONTEXT: ContextVar[HookRuntime | None] = ContextVar("sayacode_hook_runtime", default=None)


def _active_runtime() -> HookRuntime:
    return _RUNTIME_CONTEXT.get() or _RUNTIME


def create_hook_runtime(workspace: str | Path) -> HookRuntime:
    """Create a runtime-scoped hook engine for one workspace."""
    runtime = HookRuntime()
    runtime.configure_workspace(workspace)
    return runtime


@contextmanager
def hook_runtime_session(runtime: HookRuntime) -> Iterator[HookRuntime]:
    """Use a specific hook runtime in the current execution context."""
    token = _RUNTIME_CONTEXT.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_CONTEXT.reset(token)


@contextmanager
def hook_workspace_session(workspace: str | Path) -> Iterator[HookRuntime]:
    """Bind hook execution to one workspace for the current execution context."""
    base_runtime = _active_runtime()
    runtime = create_hook_runtime(workspace)
    runtime.audit_log = base_runtime.audit_log
    token = _RUNTIME_CONTEXT.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_CONTEXT.reset(token)


__all__ = [
    "HOOK_EVENTS",
    "HookCommand",
    "HookRunResult",
    "configure_hooks_workspace",
    "create_hook_runtime",
    "hook_runtime_session",
    "hook_workspace_session",
    "get_hook_audit_log",
    "get_hook_status",
    "get_hooks_workspace",
    "is_hook_workspace_trusted",
    "render_hook_status",
    "restore_hooks_workspace",
    "sanitize_hook_payload",
    "trigger_hook_event",
    "trust_hook_workspace",
    "untrust_hook_workspace",
]
