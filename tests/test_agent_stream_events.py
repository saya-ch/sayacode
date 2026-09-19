"""Agent 流式事件的抽取与去重。

真机背景（``CODE_REVIEW_FINDINGS.md`` 第八轮）：

* 之前图流只订阅 LangGraph 的 ``updates`` 模式 —— **节点级**输出。一次模型调用
  期间结构上不可能有任何可显示内容，用户只能盯着一个「思考中…」；
  实测有一次 grep + 模型调用等了 6 分钟。
* 改成同时订阅 ``updates`` + ``messages`` 之后，逐 token 的正文与推理都能拿到，
  但同一条回答的两个来源必须去重，否则整段回答会出现两遍。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from lib.agent_stream import AgentStreamExtractor


def _agent() -> AgentStreamExtractor:
    """只要抽取逻辑，不构造真实 agent。"""
    return AgentStreamExtractor()


def _chunk(*, content: str = "", reasoning: str = "") -> AIMessage:
    extra = {"reasoning": reasoning} if reasoning else {}
    return AIMessage(content=content, additional_kwargs=extra)


# ── 模式元组拆分 ──────────────────────────────────────────────────────────────


def test_mode_event_is_split():
    mode, payload = AgentStreamExtractor.split_mode_event(("messages", ("payload", {})))

    assert mode == "messages"
    assert payload == ("payload", {})


def test_plain_tuple_is_not_treated_as_a_mode_event():
    """恰好两个元素的普通元组不能被误判成 (mode, payload)。"""
    mode, payload = AgentStreamExtractor.split_mode_event(("hello", "world"))

    assert mode is None
    assert payload == ("hello", "world")


# ── 逐 token 通道 ─────────────────────────────────────────────────────────────


def test_reasoning_becomes_a_thinking_marker_on_the_status_channel():
    agent = _agent()

    ev = agent.extract_token_event(_chunk(reasoning="先看目录"))

    assert ev.kind == "reasoning" and ev.text == "先看目录", "思考链走状态通道，不得计入最终回复"


def test_content_becomes_plain_text():
    agent = _agent()

    ev = agent.extract_token_event(_chunk(content="答案"))

    assert ev.kind == "text" and ev.text == "答案"


def test_reasoning_wins_over_content_in_the_same_chunk():
    agent = _agent()

    ev = agent.extract_token_event(_chunk(content="答案", reasoning="思考"))

    assert ev.kind == "reasoning" and ev.text == "思考"


def test_tool_messages_never_leak_into_the_text_stream():
    """``messages`` 模式也会吐工具结果；当成正文接收会让工具输出混进回答。

    实测回归：工具返回值 ``sunny in Paris`` 曾出现在最终回复里。
    """
    agent = _agent()

    tool = ToolMessage(content="sunny in Paris", tool_call_id="call_1", name="get_weather")

    assert agent.extract_token_event(tool).text == ""


def test_human_messages_are_ignored():
    agent = _agent()

    assert agent.extract_token_event(HumanMessage(content="用户输入")).text == ""


# ── 双模式去重 ────────────────────────────────────────────────────────────────


def test_updates_text_is_skipped_once_tokens_were_streamed():
    """逐 token 已发过正文时，updates 里同一个 AI 消息不得再发一次。"""
    agent = _agent()
    agent.tokens_seen = True

    event = agent.extract_stream_delta(
        {"agent": {"messages": [AIMessage(content="完整回答")]}}
    )

    assert event is not None
    # 逐 token 已发过正文 → display_text 为空（去重生效）
    assert event.display_text == ""
    assert event.kind == "text"


def test_updates_text_is_used_when_token_stream_is_unavailable():
    """拿不到逐 token 流（旧版 LangGraph）时必须回退，而不是什么都不显示。"""
    agent = _agent()
    agent.tokens_seen = False

    event = agent.extract_stream_delta(
        {"agent": {"messages": [AIMessage(content="完整回答")]}}
    )

    assert event is not None
    assert event.display_text == "完整回答"


def test_tool_call_labels_still_come_from_updates():
    """工具调用标签必须保留 —— 逐 token 的 tool_call_chunks 拼不出干净的名字。"""
    agent = _agent()
    agent.tokens_seen = True

    message = AIMessage(
        content="",
        tool_calls=[{"name": "grep_search", "args": {}, "id": "c1", "type": "tool_call"}],
    )

    event = agent.extract_stream_delta({"agent": {"messages": [message]}})

    assert event is not None
    assert event.display_text == "[调用工具: grep_search]"
    assert event.kind == "tool_start"


def test_messages_mode_marks_that_tokens_were_streamed():
    agent = _agent()
    assert agent.tokens_seen is False

    agent.extract_stream_delta(("messages", (_chunk(content="hi"), {"langgraph_node": "agent"})))

    assert agent.tokens_seen is True


# ── 推理字段的兼容形态 ────────────────────────────────────────────────────────


def test_reasoning_details_list_is_supported():
    """部分厂商用结构化的 ``reasoning_details`` 列表。"""
    from lib.models.compat import extract_reasoning_text

    assert extract_reasoning_text({
        "reasoning_details": [{"type": "reasoning.text", "text": "甲"}]
    }) == "甲"
    assert extract_reasoning_text({"reasoning_content": "乙"}) == "乙"
    assert extract_reasoning_text({"thinking": "丙"}) == "丙"
    assert extract_reasoning_text({}) == ""
    assert extract_reasoning_text(None) == ""
    assert extract_reasoning_text(SimpleNamespace()) == ""
