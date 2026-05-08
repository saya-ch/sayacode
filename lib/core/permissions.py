"""Tool permission policy engine."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional
import json

from .audit import append_audit_event
from ..i18n import tr
from .paths import SayacodePaths
from .private_io import write_private_json


PermissionAction = str
VALID_ACTIONS = {"allow", "ask", "deny"}

READ_ONLY_TOOLS = {
    "read_file",
    "glob_search",
    "grep_search",
    "list_directory",
    "analyze_project",
    "get_project_summary",
    "list_project_files",
    "get_file_info",
    "list_symbols",
    "find_symbol",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_remote",
    "check_command_safety_tool",
    "get_system_info",
    "list_environment_variables",
    "read_output_file",
}

SAFE_WRITE_TOOLS = {
    "write_file",
    "search_replace",
    "create_directory",
    "batch_edit",
}

SAFE_GIT_TOOLS = {
    "git_add",
    "git_commit",
    "git_stash",
}

ASK_TOOLS = {
    "delete_file",
    "git_checkout",
    "git_pull",
    "git_push",
    "execute_command_tool",
}

RESTRICTED_TOOLS = SAFE_WRITE_TOOLS | ASK_TOOLS

DEFAULT_TOOL_RULES: Dict[str, PermissionAction] = {
    **{name: "allow" for name in READ_ONLY_TOOLS},
    **{name: "allow" for name in SAFE_WRITE_TOOLS},
    **{name: "allow" for name in SAFE_GIT_TOOLS},
    **{name: "allow" for name in ASK_TOOLS},
}

PATH_ARGUMENT_KEYS = {
    "path",
    "file_path",
    "directory",
    "dir_path",
    "target_path",
    "source_path",
    "src",
    "dst",
    "cwd",
}


@dataclass(frozen=True)
class PermissionRequest:
    """Permission prompt payload."""

    tool_name: str
    action: PermissionAction
    arguments_preview: str
    source: str


@dataclass(frozen=True)
class PermissionDecision:
    """Permission check result."""

    allowed: bool
    action: PermissionAction
    reason: str
    source: str


class PermissionPolicy:
    """Merged user/project tool permission policy."""

    def __init__(
        self,
        workspace: Optional[Path] = None,
        default_action: PermissionAction = "ask",
        tool_rules: Optional[Dict[str, PermissionAction]] = None,
        path_rules: Optional[Dict[str, PermissionAction]] = None,
        command_rules: Optional[Dict[str, PermissionAction]] = None,
        sources: Optional[Dict[str, str]] = None,
        path_sources: Optional[Dict[str, str]] = None,
        command_sources: Optional[Dict[str, str]] = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve() if workspace else None
        self.default_action = _normalize_action(default_action, fallback="ask")
        self.tool_rules = dict(DEFAULT_TOOL_RULES)
        if tool_rules:
            for tool_name, action in tool_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    self.tool_rules[str(tool_name)] = normalized_action
        self.path_rules: Dict[str, PermissionAction] = {}
        if path_rules:
            for pattern, action in path_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    self.path_rules[str(pattern)] = normalized_action
        self.command_rules: Dict[str, PermissionAction] = {}
        if command_rules:
            for pattern, action in command_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    self.command_rules[str(pattern)] = normalized_action
        self.sources = sources or {}
        self.path_sources = path_sources or {}
        self.command_sources = command_sources or {}

    @classmethod
    def load(cls, workspace: Optional[Path] = None) -> "PermissionPolicy":
        """Load built-in, user, and project policy layers."""
        merged_rules: Dict[str, PermissionAction] = {}
        merged_path_rules: Dict[str, PermissionAction] = {}
        merged_command_rules: Dict[str, PermissionAction] = {}
        sources: Dict[str, str] = {name: "built-in" for name in DEFAULT_TOOL_RULES}
        path_sources: Dict[str, str] = {}
        command_sources: Dict[str, str] = {}
        default_action: PermissionAction = "ask"

        for label, path in _policy_paths(workspace):
            data = _read_policy_file(path)
            if not data:
                continue

            candidate_default = _normalize_action(data.get("default"), fallback="")
            if candidate_default:
                default_action = candidate_default

            for tool_name, action in (data.get("tools") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_rules[str(tool_name)] = normalized_action
                    sources[str(tool_name)] = label

            for pattern, action in (data.get("paths") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_path_rules[str(pattern)] = normalized_action
                    path_sources[str(pattern)] = label

            for pattern, action in (data.get("commands") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_command_rules[str(pattern)] = normalized_action
                    command_sources[str(pattern)] = label

        return cls(
            workspace=workspace,
            default_action=default_action,
            tool_rules=merged_rules,
            path_rules=merged_path_rules,
            command_rules=merged_command_rules,
            sources=sources,
            path_sources=path_sources,
            command_sources=command_sources,
        )

    def decide(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> PermissionDecision:
        """Return the configured action for a tool."""
        path_decision = self._decide_path_rule(tool_name, arguments or {})
        if path_decision:
            return path_decision

        command_decision = self._decide_command_rule(tool_name, arguments or {})
        if command_decision:
            return command_decision

        action = self.tool_rules.get(tool_name, self.default_action)
        source = self.sources.get(tool_name, "default")
        return PermissionDecision(
            allowed=action == "allow",
            action=action,
            reason=f"{tool_name}: {action}",
            source=source,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize effective policy."""
        return {
            "default": self.default_action,
            "tools": dict(sorted(self.tool_rules.items())),
            "paths": dict(sorted(self.path_rules.items())),
            "commands": dict(sorted(self.command_rules.items())),
        }

    def _decide_path_rule(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[PermissionDecision]:
        if not self.path_rules:
            return None

        for value in _extract_argument_paths(arguments):
            normalized_path = _normalize_path_for_match(value)
            for pattern, action in self.path_rules.items():
                if _path_pattern_matches(pattern, normalized_path):
                    source = self.path_sources.get(pattern, "path")
                    return PermissionDecision(
                        allowed=action == "allow",
                        action=action,
                        reason=f"{tool_name}: {action} for path {pattern}",
                        source=source,
                    )
        return None

    def _decide_command_rule(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[PermissionDecision]:
        if not self.command_rules:
            return None

        command = str(arguments.get("command") or "").strip()
        if not command:
            return None

        normalized_command = command.lower()
        for pattern, action in self.command_rules.items():
            if fnmatch(normalized_command, pattern.lower()):
                source = self.command_sources.get(pattern, "command")
                return PermissionDecision(
                    allowed=action == "allow",
                    action=action,
                    reason=f"{tool_name}: {action} for command {pattern}",
                    source=source,
                )
        return None


class PermissionRuntime:
    """Process-wide runtime policy and optional interactive callback."""

    def __init__(self) -> None:
        self.workspace: Optional[Path] = None
        self.policy = PermissionPolicy.load(None)
        self.confirm_callback: Optional[Callable[[PermissionRequest], bool]] = None
        self.session_rules: Dict[str, PermissionAction] = {}
        self.session_rule_source = "session"
        self.audit_log: list[Dict[str, Any]] = []

    def configure_workspace(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.policy = PermissionPolicy.load(self.workspace)

    def set_confirm_callback(self, callback: Optional[Callable[[PermissionRequest], bool]]) -> None:
        self.confirm_callback = callback

    def set_session_rules(
        self,
        rules: Optional[Dict[str, PermissionAction]],
        source: str = "session",
    ) -> None:
        normalized_rules: Dict[str, PermissionAction] = {}
        for tool_name, action in (rules or {}).items():
            normalized_action = _normalize_action(action, fallback="")
            if normalized_action:
                normalized_rules[str(tool_name)] = normalized_action
        self.session_rules = normalized_rules
        self.session_rule_source = str(source or "session")

    def check(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> PermissionDecision:
        decision = self._decide(tool_name, arguments or {})
        if decision.action == "allow":
            self._record(tool_name, decision, arguments, allowed=True)
            return decision

        if decision.action == "deny":
            blocked = PermissionDecision(
                allowed=False,
                action="deny",
                reason=f"Permission denied for tool '{tool_name}' by {decision.source} policy.",
                source=decision.source,
            )
            self._record(tool_name, blocked, arguments, allowed=False)
            return blocked

        request = PermissionRequest(
            tool_name=tool_name,
            action=decision.action,
            arguments_preview=summarize_arguments(arguments or {}),
            source=decision.source,
        )

        if self.confirm_callback and self.confirm_callback(request):
            allowed = PermissionDecision(
                allowed=True,
                action="ask",
                reason=f"Permission granted for tool '{tool_name}' by user confirmation.",
                source=decision.source,
            )
            self._record(tool_name, allowed, arguments, allowed=True)
            return allowed

        denied = PermissionDecision(
            allowed=False,
            action="ask",
            reason=(
                f"Permission required for tool '{tool_name}'. "
                "Run /permissions to inspect or change tool policy."
            ),
            source=decision.source,
        )
        self._record(tool_name, denied, arguments, allowed=False)
        return denied

    def _decide(self, tool_name: str, arguments: Dict[str, Any]) -> PermissionDecision:
        if tool_name in self.session_rules:
            action = self.session_rules[tool_name]
            return PermissionDecision(
                allowed=action == "allow",
                action=action,
                reason=f"{tool_name}: {action}",
                source=self.session_rule_source,
            )
        for pattern, action in self.session_rules.items():
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return PermissionDecision(
                    allowed=action == "allow",
                    action=action,
                    reason=f"{tool_name}: {action}",
                    source=self.session_rule_source,
                )
        return self.policy.decide(tool_name, arguments)

    def _record(
        self,
        tool_name: str,
        decision: PermissionDecision,
        arguments: Optional[Dict[str, Any]],
        allowed: bool,
    ) -> None:
        entry = {
            "tool": tool_name,
            "action": decision.action,
            "allowed": allowed,
            "source": decision.source,
            "arguments": summarize_arguments(arguments or {}),
        }
        self.audit_log.append(entry)
        if len(self.audit_log) > 200:
            self.audit_log = self.audit_log[-100:]
        append_audit_event(
            "permission",
            tool_name,
            workspace=self.workspace,
            allowed=allowed,
            details=entry,
        )


def summarize_arguments(arguments: Dict[str, Any]) -> str:
    """Create a compact, redacted argument preview."""
    if not arguments:
        return "{}"

    redacted: Dict[str, Any] = {}
    for key, value in arguments.items():
        key_text = str(key)
        if _is_sensitive_key(key_text):
            redacted[key_text] = "***"
            continue
        value_text = str(value)
        redacted[key_text] = value_text[:160] + ("..." if len(value_text) > 160 else "")

    return json.dumps(redacted, ensure_ascii=False, sort_keys=True)


def _policy_paths(workspace: Optional[Path]) -> list[tuple[str, Path]]:
    sayacode_paths = SayacodePaths.resolve(create=False)
    paths = [("user", sayacode_paths.user_permissions)]
    if workspace:
        paths.append(("project", sayacode_paths.project_permissions(workspace)))
    return paths


def _read_policy_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_action(value: Any, fallback: PermissionAction) -> PermissionAction:
    action = str(value or "").strip().lower()
    return action if action in VALID_ACTIONS else fallback


def _is_sensitive_key(key: str) -> bool:
    normalized = key.upper()
    return any(marker in normalized for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"))


def _extract_argument_paths(arguments: Dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key, value in arguments.items():
        if str(key) not in PATH_ARGUMENT_KEYS:
            continue
        if isinstance(value, (list, tuple, set)):
            paths.extend(str(item) for item in value if item)
        elif value:
            paths.append(str(value))
    return paths


def _normalize_path_for_match(path: str) -> str:
    return str(path).replace("\\", "/").strip()


def _path_pattern_matches(pattern: str, path: str) -> bool:
    normalized_pattern = _normalize_path_for_match(pattern)
    candidates = {path, path.lower()}
    pattern_candidates = {normalized_pattern, normalized_pattern.lower()}
    return any(
        fnmatch(candidate, pattern_candidate)
        for candidate in candidates
        for pattern_candidate in pattern_candidates
    )


_RUNTIME = PermissionRuntime()
_RUNTIME_CONTEXT: ContextVar[PermissionRuntime | None] = ContextVar(
    "sayacode_permission_runtime",
    default=None,
)


def _active_runtime() -> PermissionRuntime:
    return _RUNTIME_CONTEXT.get() or _RUNTIME


def create_permission_runtime(workspace: str | Path) -> PermissionRuntime:
    """Create a runtime-scoped permission engine for one workspace."""
    base_runtime = _active_runtime()
    runtime = PermissionRuntime()
    runtime.configure_workspace(workspace)
    runtime.confirm_callback = base_runtime.confirm_callback
    runtime.session_rules = dict(base_runtime.session_rules)
    runtime.session_rule_source = base_runtime.session_rule_source
    return runtime


@contextmanager
def permission_runtime_session(runtime: PermissionRuntime) -> Iterator[PermissionRuntime]:
    """Use a specific permission runtime in the current execution context."""
    token = _RUNTIME_CONTEXT.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_CONTEXT.reset(token)


@contextmanager
def permission_workspace_session(workspace: str | Path) -> Iterator[PermissionRuntime]:
    """Bind permission checks to one workspace for the current execution context."""
    base_runtime = _active_runtime()
    runtime = create_permission_runtime(workspace)
    runtime.audit_log = base_runtime.audit_log
    token = _RUNTIME_CONTEXT.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_CONTEXT.reset(token)


def configure_permission_workspace(workspace: str | Path) -> None:
    """Reload permission policy for a workspace."""
    _active_runtime().configure_workspace(workspace)


def get_permission_workspace() -> Optional[Path]:
    """Return the active permission workspace."""
    return _active_runtime().workspace


def restore_permission_workspace(workspace: Optional[str | Path]) -> None:
    """Restore permission runtime to a previous workspace."""
    runtime = _active_runtime()
    if workspace is None:
        runtime.workspace = None
        runtime.policy = PermissionPolicy.load(None)
        return
    runtime.configure_workspace(workspace)


def set_permission_confirm_callback(
    callback: Optional[Callable[[PermissionRequest], bool]]
) -> None:
    """Set the interactive confirmation callback."""
    _active_runtime().set_confirm_callback(callback)


def set_session_permission_rules(
    rules: Optional[Dict[str, PermissionAction]],
    source: str = "session",
) -> None:
    """Set in-memory permission rules with highest precedence."""
    _active_runtime().set_session_rules(rules, source=source)


def enforce_tool_permission(tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Return None when allowed, otherwise a user-facing denial message."""
    decision = _active_runtime().check(tool_name, arguments)
    if decision.allowed:
        return None
    return f"⚠️ {decision.reason}"


def get_permission_policy_summary() -> str:
    """Render the effective policy for CLI display."""
    runtime = _active_runtime()
    policy = runtime.policy
    lines = [
        tr("permission_policy.title"),
        tr("permission_policy.workspace", workspace=policy.workspace or "none"),
        tr("permission_policy.default", action=policy.default_action),
    ]
    if runtime.session_rules:
        lines.extend([
            "",
            tr("permission_policy.session_overrides", source=runtime.session_rule_source),
        ])
        for tool_name, action in sorted(runtime.session_rules.items()):
            lines.append(f"  {tool_name}: {action}")

    lines.extend([
        "",
        tr("permission_policy.tools"),
    ])
    for tool_name, action in sorted(policy.tool_rules.items()):
        source = policy.sources.get(tool_name, "built-in")
        lines.append(f"  {tool_name}: {action} ({source})")
    if policy.path_rules:
        lines.extend(["", tr("permission_policy.paths")])
        for pattern, action in sorted(policy.path_rules.items()):
            source = policy.path_sources.get(pattern, "policy")
            lines.append(f"  {pattern}: {action} ({source})")
    if policy.command_rules:
        lines.extend(["", tr("permission_policy.commands")])
        for pattern, action in sorted(policy.command_rules.items()):
            source = policy.command_sources.get(pattern, "policy")
            lines.append(f"  {pattern}: {action} ({source})")
    return "\n".join(lines)


def set_tool_permission(tool_name: str, action: PermissionAction, scope: str = "user") -> Path:
    """Persist one tool permission in user or project scope."""
    runtime = _active_runtime()
    normalized_action = _normalize_action(action, fallback="")
    if not normalized_action:
        raise ValueError("action must be one of: allow, ask, deny")

    if scope not in {"user", "project"}:
        raise ValueError("scope must be user or project")

    if scope == "project":
        if runtime.workspace is None:
            raise ValueError("project scope requires a workspace")
        path = SayacodePaths.resolve(create=False).project_permissions(runtime.workspace)
    else:
        path = SayacodePaths.resolve(create=True).user_permissions

    data = _read_policy_file(path) or {"default": "ask", "tools": {}}
    tools = data.setdefault("tools", {})
    tools[str(tool_name)] = normalized_action
    write_private_json(path, data)
    if runtime.workspace is not None:
        runtime.configure_workspace(runtime.workspace)
    else:
        runtime.policy = PermissionPolicy.load(None)
    append_audit_event(
        "permission_policy",
        "set_tool_permission",
        workspace=runtime.workspace,
        allowed=True,
        details={"tool": tool_name, "action": normalized_action, "scope": scope, "path": str(path)},
    )
    return path


def get_permission_audit_log() -> list[Dict[str, Any]]:
    """Return recent in-process permission decisions."""
    return list(_active_runtime().audit_log)


__all__ = [
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRequest",
    "configure_permission_workspace",
    "create_permission_runtime",
    "RESTRICTED_TOOLS",
    "enforce_tool_permission",
    "get_permission_audit_log",
    "get_permission_policy_summary",
    "get_permission_workspace",
    "permission_runtime_session",
    "permission_workspace_session",
    "restore_permission_workspace",
    "set_permission_confirm_callback",
    "set_session_permission_rules",
    "set_tool_permission",
    "summarize_arguments",
]
