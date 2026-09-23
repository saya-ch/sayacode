"""在原生图状态中标记真实用户轮次，不复制对话正文。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from langchain.agents.middleware.types import AgentMiddleware, AgentState, OmitFromInput
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from typing_extensions import NotRequired

from ..agent.context import AgentContext


class MemoryTurnState(AgentState):
    """保存本轮来源身份和当时的学习设置，由 checkpoint 持久化。"""

    memory_turn: Annotated[NotRequired[dict[str, Any]], OmitFromInput]


class MemoryTurnMiddleware(AgentMiddleware):
    """每条真实用户消息只生成一次学习来源标记。"""

    state_schema = MemoryTurnState

    async def abefore_agent(
        self, state: Mapping[str, Any], runtime: Runtime[None]
    ) -> dict[str, Any] | None:
        context = cast(AgentContext | None, runtime.context)
        if context is None:
            return None
        messages = state.get("messages", ())
        if not isinstance(messages, (list, tuple)):
            return None
        last_user: HumanMessage | None = None
        for message in reversed(messages):
            if not isinstance(message, HumanMessage):
                continue
            if message.additional_kwargs.get("sayacode_source"):
                continue
            last_user = message
            break
        if last_user is None:
            return None
        identity = str(last_user.id or "").strip()
        if not identity:
            # 部分自定义模型不会为消息指定 ID；此指纹仅用于同一线程重试去重。
            raw = f"{context.session_id}:{len(messages)}:{last_user.content}"
            identity = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        current = state.get("memory_turn")
        if isinstance(current, Mapping) and current.get("message_id") == identity:
            return None
        eligible = (
            context.memory_learning_enabled
            and context.trust_level != "read_only"
            and not context.is_background
            and context.task_id is None
        )
        return {
            "memory_turn": {
                "message_id": identity,
                "thread_id": context.session_id,
                "project_id": context.memory_project_id,
                "owner_id": context.memory_owner_id,
                "eligible": eligible,
                "profile_name": context.memory_profile_name,
                "model_identity_sha256": context.memory_model_identity_sha256,
                "received_at": datetime.now(UTC).isoformat(),
            }
        }


__all__ = ["MemoryTurnMiddleware", "MemoryTurnState"]
