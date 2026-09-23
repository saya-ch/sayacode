"""从原生检查点历史重建一轮对话的有限提取依据。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from ..agent.context import AgentHandle
from ..agent.runtime import AgentRuntime
from .records import MemorySource

_LOCAL_EVIDENCE_TOOLS = frozenset(
    {"read_file", "list_directory", "git", "analyze_project", "list_symbols", "execute_command_tool"}
)


def _identity(message: AnyMessage, position: int) -> str:
    """官方消息已有 ID 时直接用它，缺失时构造稳定的轮内标识。"""
    if message.id:
        return str(message.id)
    material = f"{position}:{message.type}:{message.content}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TurnEvidence:
    """源消息与实际验证过的本地工具结果。"""

    messages: tuple[AnyMessage, ...]
    source_ids: tuple[str, ...]
    verified_source_ids: tuple[str, ...]
    tool_calls: dict[str, dict[str, Any]]


async def collect_turn_evidence(
    runtime: AgentRuntime,
    handle: AgentHandle,
    source: MemorySource,
    *,
    user_message_id: str,
) -> TurnEvidence:
    """沿当前 checkpoint 分支向前找用户原话，再依消息 ID 去重重建本轮。"""
    if not source.checkpoint_id or not user_message_id:
        raise ValueError("记忆来源缺少 checkpoint 或用户消息 ID")
    config = runtime.thread_config(source.thread_id)
    config["configurable"]["checkpoint_id"] = source.checkpoint_id
    snapshots = []
    found = False
    async for snapshot in handle.graph.aget_state_history(config):
        snapshots.append(snapshot)
        if any(
            isinstance(message, HumanMessage) and str(message.id or "") == user_message_id
            for message in snapshot.values.get("messages", ())
        ):
            found = True
            break
    if not found:
        raise ValueError("原始用户消息已不在检查点历史中，不能据摘要提取记忆")

    # 最早一张快照可能包含先前轮次；它们的 ID 不属于本轮。
    earliest: Sequence[AnyMessage] = snapshots[-1].values.get("messages", ())
    start = next(
        index
        for index, message in enumerate(earliest)
        if isinstance(message, HumanMessage) and str(message.id or "") == user_message_id
    )
    prior_ids = {_identity(message, index) for index, message in enumerate(earliest[:start])}
    seen = set(prior_ids)
    ordered: list[AnyMessage] = []
    for snapshot in reversed(snapshots):
        for index, message in enumerate(snapshot.values.get("messages", ())):
            identity = _identity(message, index)
            if identity in seen:
                continue
            seen.add(identity)
            ordered.append(message)

    latest_user: HumanMessage | None = None
    last_answer: AIMessage | None = None
    evidence_tools: list[ToolMessage] = []
    calls: dict[str, dict[str, Any]] = {}
    for message in ordered:
        if isinstance(message, HumanMessage):
            if str(message.id or "") == user_message_id:
                latest_user = message
        elif isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[str(call.get("id") or "")] = dict(call)
            if not message.tool_calls and message.text.strip():
                last_answer = message
        elif isinstance(message, ToolMessage) and message.name in _LOCAL_EVIDENCE_TOOLS:
            evidence_tools.append(message)
    if latest_user is None:
        raise ValueError("原始用户消息与检查点引用不匹配")

    selected: list[AnyMessage] = [latest_user]
    selected.extend(evidence_tools)
    if last_answer is not None:
        selected.append(last_answer)
    selected_ids = tuple(_identity(message, index) for index, message in enumerate(selected))
    # 工具成功只证明调用发生，不能自动证明输出中的陈述真实。
    verified: tuple[str, ...] = ()
    return TurnEvidence(tuple(selected), selected_ids, verified, calls)


__all__ = ["TurnEvidence", "collect_turn_evidence"]
