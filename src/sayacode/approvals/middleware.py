"""Jev 预审、官方 HITL 条件中断与执行前静态复核。"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    OmitFromInput,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from typing_extensions import NotRequired

from ..paths import context_value
from .jev import ToolReview, ToolReviewer
from .policy import PolicyDecision, policy_for

ReviewCallback = Callable[[list[ToolReview], Runtime[Any]], Awaitable[None] | None]


class ToolReviewState(AgentState):
    """图内当前批次的工具审理结果。"""

    tool_reviews: Annotated[NotRequired[dict[str, ToolReview]], OmitFromInput]


def decision_for(request: ToolCallRequest) -> PolicyDecision:
    """对原生工具请求执行静态权限判定。"""
    return policy_for(request.runtime.context).decide(
        request.tool_call["name"], request.tool_call.get("args", {}), request.runtime.context
    )


def _review_for(request: ToolCallRequest) -> Mapping[str, Any] | None:
    state = request.state
    if not isinstance(state, Mapping):
        return None
    reviews = state.get("tool_reviews")
    if not isinstance(reviews, Mapping):
        return None
    review = reviews.get(str(request.tool_call.get("id") or ""))
    if not isinstance(review, Mapping) or review.get("tool_name") != request.tool_call.get("name"):
        return None
    expected = hashlib.sha256(
        json.dumps(
            request.tool_call.get("args", {}),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return review if review.get("arguments_sha256") == expected else None


def _requires_human(request: ToolCallRequest) -> bool:
    if decision_for(request).action != "ask":
        return False
    if context_value(request.runtime.context, "trust_level") != "jev":
        return True
    review = _review_for(request)
    return review is None or review.get("action") not in {"allow", "deny"}


class PolicyMiddleware(AgentMiddleware):
    """在工具执行前复核静态拒绝和 Jev 的高置信度拒绝。"""

    @staticmethod
    def _denied(request: ToolCallRequest) -> ToolMessage | None:
        decision = decision_for(request)
        review = _review_for(request)
        jev_denied = (
            context_value(request.runtime.context, "trust_level") == "jev"
            and review is not None
            and review.get("action") == "deny"
        )
        if decision.action != "deny" and not jev_denied:
            return None
        reason = str(review.get("reason")) if jev_denied and review is not None else decision.reason
        source = "jev" if jev_denied else decision.source
        return ToolMessage(
            content=reason,
            status="error",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            artifact={
                "action": "deny",
                "source": source,
                **(
                    {
                        "confidence": review.get("confidence"),
                        "model": review.get("model"),
                        "request_id": review.get("request_id"),
                    }
                    if jev_denied and review is not None
                    else {}
                ),
            },
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        denied = self._denied(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        denied = self._denied(request)
        return denied if denied is not None else await handler(request)


class JevReviewMiddleware(AgentMiddleware):
    """只在 Jev 信任档审理原本需要人工批准的完整调用。"""

    state_schema = ToolReviewState

    def __init__(self, reviewer: ToolReviewer, *, on_reviews: ReviewCallback | None = None) -> None:
        super().__init__()
        self.reviewer = reviewer
        self.on_reviews = on_reviews

    async def aafter_model(
        self, state: Mapping[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        context = runtime.context
        if getattr(context, "trust_level", None) != "jev":
            return None
        messages = list(state.get("messages", []))
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        candidates = [
            call
            for call in messages[-1].tool_calls
            if policy_for(context)
            .decide(str(call.get("name") or ""), call.get("args", {}), context)
            .action
            == "ask"
        ]
        if not candidates:
            return {"tool_reviews": {}}
        try:
            reviews = await self.reviewer.review(candidates, state)
        except Exception as exc:
            reviews = {
                str(call.get("id") or ""): {
                    "tool_call_id": str(call.get("id") or ""),
                    "tool_name": str(call.get("name") or "tool"),
                    "arguments_sha256": hashlib.sha256(
                        json.dumps(
                            call.get("args", {}),
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest(),
                    "action": "ask",
                    "confidence": 0.0,
                    "model": None,
                    "request_id": None,
                    "reason": f"Jev 审理不可用，已转人工确认：{type(exc).__name__}",
                }
                for call in candidates
            }
        if self.on_reviews is not None:
            result = self.on_reviews(list(reviews.values()), runtime)
            if inspect.isawaitable(result):
                await result
        return {"tool_reviews": reviews}


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
    "JevReviewMiddleware",
    "PolicyMiddleware",
    "ToolReviewState",
    "build_approval_middleware",
    "decision_for",
]
