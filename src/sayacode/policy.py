"""Product authorization rules connected to LangChain's official HITL middleware."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

Action = Literal["allow", "ask", "deny"]
READ_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "glob_search",
        "grep_search",
        "read_output_file",
        "get_system_info",
        "list_environment_variables",
        "analyze_project",
        "get_project_summary",
        "list_project_files",
        "get_file_info",
        "list_symbols",
        "find_symbol",
        "check_command_safety_tool",
        "write_todos",
        "task_status",
        "task_wait",
        "task_delivery",
    }
)
EDIT_TOOLS = frozenset({"write_file", "search_replace", "batch_edit", "create_directory"})
GIT_READ_COMMANDS = frozenset({"status", "diff", "log", "branch", "remote", "show"})
PATH_ARGUMENTS = frozenset({"path", "file_path", "root_dir", "cwd"})
LOCAL_PATH_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "search_replace",
        "batch_edit",
        "create_directory",
        "delete_file",
        "list_directory",
        "glob_search",
        "grep_search",
        "read_output_file",
        "execute_command_tool",
        "git",
        "analyze_project",
        "get_project_summary",
        "list_project_files",
        "get_file_info",
        "list_symbols",
        "find_symbol",
    }
)
_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template", ".env.dist"}


def context_value(context: Any, name: str, default: Any = None) -> Any:
    """Read the application's context without depending on its implementation."""
    return (
        context.get(name, default)
        if isinstance(context, Mapping)
        else getattr(context, name, default)
    )


def workspace_path(context: Any, value: str | Path = ".") -> Path:
    """Resolve a path against the workspace, including symlink targets."""
    root = Path(context_value(context, "workspace", Path.cwd())).expanduser().resolve()
    candidate = Path(value).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError(f"Path is outside the workspace: {value}")
    return resolved


def is_sensitive_path(path: Path) -> bool:
    if any(part.lower() in {".ssh", ".gnupg", ".aws"} for part in path.parts):
        return True
    name = path.name.lower()
    return (
        (name.startswith(".env") and name not in _TEMPLATE_NAMES)
        or name in {".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json"}
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
    )


def _normalized_arguments(name: str, arguments: Mapping[str, Any], context: Any) -> dict[str, Any]:
    normalized = dict(arguments)
    if name in LOCAL_PATH_TOOLS:
        for key in PATH_ARGUMENTS:
            if value := normalized.get(key):
                text = (
                    str(value).lstrip("/") if name in {"glob_search", "grep_search"} else str(value)
                )
                normalized[key] = str(workspace_path(context, text))
        if name in {"execute_command_tool", "git"}:
            normalized.setdefault("cwd", str(workspace_path(context)))
        if name == "git" and isinstance(normalized.get("paths"), list):
            normalized["paths"] = [
                str(workspace_path(context, str(path))) for path in normalized["paths"]
            ]
        if name == "batch_edit":
            normalized["edits"] = [
                {**edit, "path": str(workspace_path(context, edit["path"]))}
                for edit in normalized.get("edits", [])
            ]
    return normalized


def _call_key(name: str, arguments: Mapping[str, Any], context: Any) -> str:
    identity = json.dumps(
        {
            "tool": name,
            "workspace": str(workspace_path(context)),
            "arguments": _normalized_arguments(name, arguments, context),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return f"@call:{name}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _argument_paths(arguments: Mapping[str, Any]) -> list[str]:
    if isinstance(arguments.get("edits"), list):
        return [str(edit["path"]) for edit in arguments["edits"] if "path" in edit]
    if isinstance(arguments.get("paths"), list) and arguments["paths"]:
        return [str(path) for path in arguments["paths"]]
    for key in ("path", "file_path", "root_dir", "cwd"):
        if arguments.get(key) is not None:
            return [str(arguments[key])]
    return []


def _rule_matches(pattern: str, name: str, arguments: Mapping[str, Any], context: Any) -> bool:
    if pattern.startswith("@call:"):
        return pattern == _call_key(name, arguments, context)
    if not pattern.startswith("@scope:"):
        return fnmatchcase(name, pattern)
    try:
        rule = json.loads(pattern[len("@scope:") :])
    except (ValueError, TypeError):
        return False
    if not isinstance(rule, dict) or not fnmatchcase(name, str(rule.get("tool", ""))):
        return False
    if "command" in rule and not fnmatchcase(str(arguments.get("command", "")), rule["command"]):
        return False
    if "path" in rule:
        paths = _argument_paths(arguments)
        if not paths:
            return False
        root = workspace_path(context)
        for path in paths:
            resolved = workspace_path(context, path)
            relative = resolved.relative_to(root).as_posix()
            if not fnmatchcase(relative, str(rule["path"]).replace("\\", "/")):
                return False
    return True


def is_read_only(name: str, arguments: Mapping[str, Any]) -> bool:
    if name in READ_TOOLS or name == "web_search":
        return True
    if name == "git":
        return arguments.get("action", "status") in GIT_READ_COMMANDS
    if name == "delegate_to_subagent":
        return arguments.get("role", "planner") in {"planner", "reviewer"}
    return False


@dataclass(frozen=True)
class PolicyDecision:
    action: Action
    reason: str
    source: str = "default"


@dataclass
class Policy:
    """Explicit denials win; other rules use session > project > user precedence.

    Mode and workspace restrictions always apply. An explicit rule controls
    authorization, while the official HITL middleware owns approval and resume.
    """

    user: dict[str, Action] = field(default_factory=dict)
    project: dict[str, Action] = field(default_factory=dict)
    session: dict[str, Action] = field(default_factory=dict)

    def set_rule(
        self,
        name: str,
        action: str,
        scope: str = "session",
        *,
        path: str | None = None,
        command: str | None = None,
    ) -> None:
        if action not in {"allow", "ask", "deny"}:
            raise ValueError("Action must be allow, ask, or deny")
        if scope not in {"user", "project", "session"}:
            raise ValueError("Scope must be user, project, or session")
        if path is not None or command is not None:
            selector = {"tool": name}
            if path is not None:
                selector["path"] = path
            if command is not None:
                selector["command"] = command
            name = "@scope:" + json.dumps(selector, sort_keys=True, separators=(",", ":"))
        getattr(self, scope)[name] = action

    def grant_call(
        self, name: str, arguments: Mapping[str, Any], context: Any, scope: str = "session"
    ) -> str:
        """Remember approval for this workspace and exact call, without storing its secrets."""
        key = _call_key(name, arguments, context)
        self.set_rule(key, "allow", scope)
        return key

    def decide(self, name: str, arguments: Mapping[str, Any], context: Any) -> PolicyDecision:
        mode = context_value(context, "mode", "build")
        if mode not in {"build", "plan", "review"}:
            return PolicyDecision("deny", f"Unknown mode: {mode}", "mode")
        if mode != "build" and not is_read_only(name, arguments):
            return PolicyDecision("deny", f"{name} is unavailable in {mode} mode", "mode")
        try:
            # Output files use a separate, explicitly configured artifact root.
            if name in LOCAL_PATH_TOOLS and name != "read_output_file":
                for key in PATH_ARGUMENTS:
                    if value := arguments.get(key):
                        normalized = (
                            str(value).lstrip("/")
                            if name in {"glob_search", "grep_search"}
                            else str(value)
                        )
                        target = workspace_path(context, normalized)
                        if is_sensitive_path(target):
                            return PolicyDecision("deny", f"Protected file: {value}", "path")
                for edit in arguments.get("edits", []) if name == "batch_edit" else []:
                    target = workspace_path(context, edit["path"])
                    if is_sensitive_path(target):
                        return PolicyDecision("deny", f"Protected file: {edit['path']}", "path")
        except (PermissionError, ValueError, TypeError, KeyError) as exc:
            return PolicyDecision("deny", str(exc), "path")
        matches_by_scope: list[tuple[str, list[tuple[str, str]]]] = []
        for scope in ("session", "project", "user"):
            rules = getattr(self, scope)
            try:
                matches = [
                    (pattern, value)
                    for pattern, value in rules.items()
                    if _rule_matches(pattern, name, arguments, context)
                ]
            except (PermissionError, ValueError, TypeError, KeyError) as exc:
                return PolicyDecision("deny", str(exc), "path")
            if not matches:
                continue
            for _, action in matches:
                if action not in {"allow", "ask", "deny"}:
                    return PolicyDecision("deny", f"Invalid rule for {name}", scope)
                if action == "deny":
                    return PolicyDecision("deny", f"{scope} rule: {name} → deny", scope)
            matches_by_scope.append((scope, matches))
        for scope, matches in matches_by_scope:
            action = max(
                matches,
                key=lambda item: (
                    item[0].startswith("@call:"),
                    item[0].startswith("@scope:"),
                    item[0] == name,
                    len(item[0]),
                ),
            )[1]
            return PolicyDecision(
                "allow" if action == "allow" else "ask", f"{scope} rule: {name} → {action}", scope
            )
        if (is_read_only(name, arguments) and name != "web_search") or name in EDIT_TOOLS:
            return PolicyDecision("allow", "Workspace read or edit")
        if name == "delegate_to_subagent" and arguments.get("role", "planner") == "builder":
            return PolicyDecision(
                "allow", "Builder edits an isolated worktree; applying its delivery is explicit"
            )
        return PolicyDecision("ask", "Shell, deletion, or external action requires approval")


def policy_for(context: Any) -> Policy:
    policy = context_value(context, "policy")
    return policy if isinstance(policy, Policy) else Policy()


def decision_for(request: ToolCallRequest) -> PolicyDecision:
    return policy_for(request.runtime.context).decide(
        request.tool_call["name"],
        request.tool_call.get("args", {}),
        request.runtime.context,
    )


class PolicyMiddleware(AgentMiddleware):
    """Block denied calls at the official execution boundary.

    Install together with ``build_approval_middleware``. Ask decisions are
    resolved by HITL after the model, before execution reaches this wrapper.
    """

    @staticmethod
    def _denied(request: ToolCallRequest) -> ToolMessage | None:
        decision = decision_for(request)
        if decision.action != "deny":
            return None
        return ToolMessage(
            content=decision.reason,
            status="error",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            artifact={"action": "deny", "source": decision.source},
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        denied = self._denied(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        denied = self._denied(request)
        return denied if denied is not None else await handler(request)


def build_approval_middleware(tools: list[Any]) -> HumanInTheLoopMiddleware:
    """Use the official dynamic approval predicate and batched interrupt schema."""
    # Search and todo tools are contributed by official middleware, so include
    # them even when they are absent from the application's explicit tool list.
    names = {item.name for item in tools} | {"glob_search", "grep_search", "write_todos"}
    return HumanInTheLoopMiddleware(
        interrupt_on={
            name: {
                "allowed_decisions": ["approve", "reject"],
                "when": lambda request: decision_for(request).action == "ask",
            }
            for name in names
        }
    )


__all__ = [
    "Policy",
    "PolicyDecision",
    "PolicyMiddleware",
    "build_approval_middleware",
    "context_value",
    "decision_for",
    "is_read_only",
    "is_sensitive_path",
    "policy_for",
    "workspace_path",
]
