"""摘要失败降级：占位摘要与空摘要不得替换真实历史。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from sayacode.agent.summary import ResilientSummarizationMiddleware
from tests.support import ContractModel

PLACEHOLDER = "Previous conversation was too long to summarize."


def conversation() -> list[Any]:
    return [
        HumanMessage(content="原始任务：实现模块"),
        AIMessage(content="我先读文件"),
        HumanMessage(content="请继续"),
        AIMessage(content="正在实现"),
        HumanMessage(content="最近一次提问"),
        AIMessage(content="最近一次回答"),
    ]


def middleware(content: str) -> ResilientSummarizationMiddleware:
    return ResilientSummarizationMiddleware(
        model=ContractModel(responses=[AIMessage(content=content)]),
        trigger=("messages", 4),
        keep=("messages", 2),
        trim_tokens_to_summarize=None,
    )


async def test_placeholder_summary_keeps_original_task_and_recent_messages() -> None:
    messages = conversation()
    result = await middleware(PLACEHOLDER).abefore_model({"messages": messages}, None)  # type: ignore[arg-type]
    assert result is not None
    updates = result["messages"]
    removed = {item.id for item in updates if isinstance(item, RemoveMessage)}
    # 原任务与最近两条保留，中间三条删除。
    assert messages[0].id not in removed
    assert messages[4].id not in removed
    assert messages[5].id not in removed
    assert {messages[1].id, messages[2].id, messages[3].id} <= removed
    notice = updates[-1]
    assert isinstance(notice, HumanMessage)
    assert notice.additional_kwargs["sayacode_summary_failed"] is True
    assert "too_long_to_summarize" in notice.additional_kwargs["reason"]
    # 绝不把占位文本当成摘要写回。
    assert all("Here is a summary of the conversation" not in str(item.content) for item in updates)


async def test_empty_summary_is_not_written_as_summary() -> None:
    messages = conversation()
    result = await middleware("").abefore_model({"messages": messages}, None)  # type: ignore[arg-type]
    assert result is not None
    notice = result["messages"][-1]
    assert isinstance(notice, HumanMessage)
    assert notice.additional_kwargs["sayacode_summary_failed"] is True
    assert notice.additional_kwargs["reason"] == "empty_summary"
    assert all("Here is a summary of the conversation" not in str(item.content) for item in result["messages"])


async def test_usable_summary_uses_official_path() -> None:
    messages = conversation()
    result = await middleware("真正的摘要内容").abefore_model({"messages": messages}, None)  # type: ignore[arg-type]
    assert result is not None
    updates = result["messages"]
    assert isinstance(updates[0], RemoveMessage)
    assert isinstance(updates[1], HumanMessage)
    assert "Here is a summary of the conversation to date" in updates[1].content
    assert "真正的摘要内容" in updates[1].content
    assert not any(
        getattr(item, "additional_kwargs", {}).get("sayacode_summary_failed")
        for item in updates
    )


async def test_fallback_marker_is_written_only_once() -> None:
    messages = conversation()
    first = await middleware(PLACEHOLDER).abefore_model({"messages": messages}, None)  # type: ignore[arg-type]
    assert first is not None
    messages.append(first["messages"][-1])
    second = await middleware(PLACEHOLDER).abefore_model({"messages": messages}, None)  # type: ignore[arg-type]
    assert second is None
