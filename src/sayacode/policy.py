"""会话信任等级共三种，由原生人工确认中间件支撑。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

Action = Literal["allow", "ask", "deny"]
TrustLevel = Literal["read_only", "ask", "full"]
TRUST_LEVELS = ("read_only", "ask", "full")
READ_TOOLS = frozenset({
    "read_file", "list_directory", "glob_search", "grep_search", "read_output_file",
    "analyze_project", "list_symbols", "git", "web_search", "write_todos",
    "task_status", "task_wait", "task_delivery",
})


def normalize_trust(value: str | None) -> TrustLevel:
    chosen = str(value or "ask").strip().lower().replace("-", "_")
    chosen = {"只读": "read_only", "询问": "ask", "完全信任": "full"}.get(chosen, chosen)
    if chosen not in TRUST_LEVELS:
        raise ValueError(f"Unknown trust level: {value}")
    return cast(TrustLevel, chosen)


def context_value(context: Any, name: str, default: Any = None) -> Any:
    return context.get(name, default) if isinstance(context, Mapping) else getattr(context, name, default)


def workspace_path(context: Any, value: str | Path = ".") -> Path:
    """以工作区为基准解析相对路径，绝对路径视为全局。"""
    root = Path(context_value(context, "workspace", Path.cwd())).expanduser().resolve()
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def _call_key(name: str, arguments: Mapping[str, Any], context: Any) -> str:
    normalized = dict(arguments)
    for key in ("path", "file_path", "root_dir", "cwd"):
        if value := normalized.get(key):
            normalized[key] = str(workspace_path(context, str(value)))
    identity = json.dumps(
        {"tool": name, "workspace": str(workspace_path(context)), "arguments": normalized},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def is_read_only(name: str, arguments: Mapping[str, Any]) -> bool:
    if name in READ_TOOLS:
        return True
    return name == "delegate_to_subagent" and arguments.get("role", "planner") in {
        "planner", "reviewer",
    }


@dataclass(frozen=True)
class PolicyDecision:
    action: Action
    reason: str
    source: str = "trust"


@dataclass
class Policy:
    """记录单会话信任等级和已批准的精确调用。"""

    trust_level: TrustLevel = "ask"
    session_grants: set[str] = field(default_factory=set)

    def grant_call(self, name: str, arguments: Mapping[str, Any], context: Any) -> str:
        key = _call_key(name, arguments, context)
        self.session_grants.add(key)
        return key

    def decide(self, name: str, arguments: Mapping[str, Any], context: Any) -> PolicyDecision:
        level = self.trust_level
        if level == "full":
            return PolicyDecision("allow", "Full trust")
        if is_read_only(name, arguments):
            return PolicyDecision("allow", "Read-only tool")
        if level == "read_only":
            if name == "execute_command_tool":
                return PolicyDecision("ask", "Shell requires approval even in read-only trust")
            return PolicyDecision("deny", "Tool unavailable in read-only trust")
        if _call_key(name, arguments, context) in self.session_grants:
            return PolicyDecision("allow", "Exact call approved for this session", "session")
        return PolicyDecision("ask", "Side-effecting tool requires approval")


def policy_for(context: Any) -> Policy:
    policy = context_value(context, "policy")
    return policy if isinstance(policy, Policy) else Policy()


def decision_for(request: ToolCallRequest) -> PolicyDecision:
    return policy_for(request.runtime.context).decide(
        request.tool_call["name"], request.tool_call.get("args", {}), request.runtime.context
    )


class PolicyMiddleware(AgentMiddleware):
    """在原生确认之后，于执行阶段复核被拒绝的调用。"""

    @staticmethod
    def _denied(request: ToolCallRequest) -> ToolMessage | None:
        decision = decision_for(request)
        if decision.action != "deny":
            return None
        return ToolMessage(
            content=decision.reason, status="error", name=request.tool_call["name"],
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
    names = {item.name for item in tools} | {"glob_search", "grep_search", "write_todos"}
    return HumanInTheLoopMiddleware(interrupt_on={
        name: {
            "allowed_decisions": ["approve", "reject"],
            "when": lambda request: decision_for(request).action == "ask",
        }
        for name in names
    })


__all__ = [
    "Policy", "PolicyDecision", "PolicyMiddleware", "TrustLevel", "TRUST_LEVELS",
    "READ_TOOLS", "build_approval_middleware", "context_value", "decision_for",
    "is_read_only", "normalize_trust", "policy_for", "workspace_path",
]
