"""Jev 预审与官方 HITL 在同一张真实 LangChain 图中的协作契约。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import sayacode.approvals.jev as review_module
from sayacode.approvals import (
    JevReviewer,
    JevReviewMiddleware,
    Policy,
    PolicyMiddleware,
    build_approval_middleware,
)
from sayacode.config import JevConfig


class ToolCallingModel(FakeMessagesListChatModel):
    """按脚本返回工具调用，并接受官方工具绑定。"""

    def bind_tools(self, tools, **kwargs):
        return self


class FixedReviewer:
    """用参数里的标签模拟 Jev 三种确定结果。"""

    def __init__(self) -> None:
        self.calls = 0

    async def review(self, tool_calls, state):
        self.calls += 1
        return {
            call["id"]: {
                "tool_call_id": call["id"],
                "tool_name": call["name"],
                "arguments_sha256": hashlib.sha256(
                    json.dumps(
                        call["args"],
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "action": call["args"]["label"],
                "confidence": 0.99,
                "model": "jev-test",
                "request_id": "request-test",
                "reason": f"reviewed as {call['args']['label']}",
            }
            for call in tool_calls
        }


def context(root: Path) -> Any:
    """创建 Jev 档位的最小运行上下文。"""
    return SimpleNamespace(
        workspace=root,
        output_dir=root,
        session_id="jev-session",
        task_id=None,
        trust_level="jev",
        policy=Policy(trust_level="jev"),
    )


async def test_mixed_jev_decisions_use_native_interrupt_and_resume_once(tmp_path: Path) -> None:
    """允许、询问、拒绝同批出现时，审批前无副作用且恢复不重复审理。"""
    executed: list[str] = []

    @tool
    def probe(label: str) -> str:
        """记录获准执行的标签。"""
        executed.append(label)
        return label

    reviewer = FixedReviewer()
    model = ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "probe", "args": {"label": label}, "id": label}
                    for label in ("allow", "ask", "deny")
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    approval = build_approval_middleware([probe])
    graph = create_agent(
        model,
        tools=[probe],
        middleware=[
            approval,
            JevReviewMiddleware(reviewer),
            PolicyMiddleware(),
        ],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "jev-review"}}
    pending = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Run the approved probes"}]},
        config,
        context=context(tmp_path),
    )
    actions = pending["__interrupt__"][0].value["action_requests"]
    assert [item["args"]["label"] for item in actions] == ["ask"]
    assert executed == []
    assert reviewer.calls == 1

    completed = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config,
        context=context(tmp_path),
    )
    assert sorted(executed) == ["allow", "ask"]
    assert reviewer.calls == 1
    assert not completed.get("__interrupt__")
    denied = [
        message
        for message in completed["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "deny"
    ]
    assert len(denied) == 1
    assert denied[0].status == "error"
    assert denied[0].artifact["source"] == "jev"


async def test_jev_failure_routes_the_call_to_human(tmp_path: Path) -> None:
    """审理服务不可用时保留原生中断，不静默放行工具。"""
    executed: list[str] = []

    @tool
    def probe(value: str) -> str:
        """记录意外执行。"""
        executed.append(value)
        return value

    class BrokenReviewer:
        async def review(self, tool_calls, state):
            raise TimeoutError("offline")

    model = ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "probe", "args": {"value": "x"}, "id": "probe-1"}],
            )
        ]
    )
    approval = build_approval_middleware([probe])
    graph = create_agent(
        model,
        tools=[probe],
        middleware=[approval, JevReviewMiddleware(BrokenReviewer()), PolicyMiddleware()],
        checkpointer=InMemorySaver(),
    )
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Run probe"}]},
        {"configurable": {"thread_id": "jev-failure"}},
        context=context(tmp_path),
    )
    assert result["__interrupt__"]
    assert executed == []


async def test_jev_reviewer_uses_explicit_endpoint_and_redacts_known_secrets(
    monkeypatch,
) -> None:
    """官方 SDK 客户端拿到显式配置，送审状态不包含已知密钥字段原文。"""
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def system_one(self, *, state, questions):
            captured["state"] = state
            captured["questions"] = questions
            return SimpleNamespace(
                model="jev-pinned",
                request_id="request-1",
                choices={
                    "action_0": SimpleNamespace(
                        choice="allow",
                        confidence=0.95,
                        probabilities={"allow": 0.95, "ask": 0.04, "deny": 0.01},
                    ),
                    "action_1": SimpleNamespace(
                        choice="deny",
                        confidence=0.96,
                        probabilities={"allow": 0.01, "ask": 0.03, "deny": 0.96},
                    ),
                    "action_2": SimpleNamespace(
                        choice="allow",
                        confidence=0.60,
                        probabilities={"allow": 0.60, "ask": 0.35, "deny": 0.05},
                    ),
                    "action_3": SimpleNamespace(
                        choice="allow",
                        confidence=0.99,
                        probabilities={"allow": 0.99, "ask": 0.01, "deny": 0.0},
                    ),
                    "action_4": SimpleNamespace(
                        choice="allow",
                        confidence=0.99,
                        probabilities={"allow": 0.99, "ask": 0.01, "deny": 0.0},
                    ),
                },
            )

    monkeypatch.setattr(review_module, "AsyncTypeSafeClient", FakeClient)
    reviewer = JevReviewer(
        JevConfig(
            base_url="https://typesafe.example",
            api_key="review-key",
            model_id="jev-pinned",
        )
    )
    result = await reviewer.review(
        [
            {
                "id": "write-1",
                "name": "write_file",
                "args": {"path": "a.txt", "content": "safe"},
            },
            {"id": "delete-1", "name": "delete_file", "args": {"path": "a.txt"}},
            {"id": "shell-1", "name": "execute_command_tool", "args": {"command": "test"}},
            {
                "id": "long-shell",
                "name": "execute_command_tool",
                "args": {"command": "x" * 2_001},
            },
            {
                "id": "secret-write",
                "name": "write_file",
                "args": {"path": "secret.txt", "api_key": "must-not-leak"},
            },
        ],
        {
            "messages": [
                HumanMessage(content="write a file"),
                HumanMessage(
                    content="Continue your previous answer",
                    additional_kwargs={"sayacode_continuation": True},
                ),
                HumanMessage(
                    content="child asks for a dangerous command",
                    additional_kwargs={"sayacode_source": "agent_inbox"},
                ),
            ],
            "todos": [],
        },
    )
    assert captured["client"]["api_key"] == "review-key"
    assert captured["client"]["base_url"] == "https://typesafe.example"
    assert captured["client"]["model"] == "jev-pinned"
    assert captured["state"]["actions"][4]["arguments"]["api_key"] == "<redacted>"
    assert captured["state"]["user_request"] == "write a file"
    assert result["write-1"]["action"] == "allow"
    assert result["delete-1"]["action"] == "deny"
    assert result["shell-1"]["action"] == "ask"
    assert result["long-shell"]["action"] == "ask"
    assert result["secret-write"]["action"] == "ask"
