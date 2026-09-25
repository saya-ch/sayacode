"""官方 HITL 条件中断与执行前静态复核。"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from .policy import PolicyDecision, policy_for


def decision_for(request: ToolCallRequest) -> PolicyDecision:
    """对原生工具请求执行静态权限判定。"""
    return policy_for(request.runtime.context).decide(
        request.tool_call["name"], request.tool_call.get("args", {}), request.runtime.context
    )


def _requires_human(request: ToolCallRequest) -> bool:
    return decision_for(request).action == "ask"


class PolicyMiddleware(AgentMiddleware):
    """在工具执行前复核静态拒绝。"""

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
    """按真实工具目录装配官方 HITL 条件中断。"""
    names = {item.name for item in tools} | {"glob_search", "grep_search", "write_todos"}
    return HumanInTheLoopMiddleware(
        interrupt_on={
            name: {
                "allowed_decisions": ["approve", "reject"],
                "when": _requires_human,
            }
            for name in names
        }
    )


__all__ = [
    "PolicyMiddleware",
    "build_approval_middleware",
    "decision_for",
]
