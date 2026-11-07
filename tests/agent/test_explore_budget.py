"""回归：探索预算护栏（只读不写时提醒落笔）。

动机（2026-11-07 实测，见 docs/FAILURE-MODE-EXPLORE-ONLY-20261107.md）：
长上下文 A/B 的 12 个 trial 里，**2 个从头到尾一次编辑都没有**
（46 次 / 13 次全是 read/grep/glob，最后产 0 字节 patch），
另外 3 个到第 29–42 个动作才首次落笔；通过组的对应位置大多在前 10 个动作内。
失败组模型调用中位数是通过组的 3.7 倍。

这不是「不会做」——同样 6 题在短上下文下 12/12 全过——而是缺少收敛机制。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sayacode.agent.graph import (
    _EXPLORE_BUDGET_KEY,
    ExploreBudgetMiddleware,
)


def _human(text: str = "fix the bug") -> HumanMessage:
    return HumanMessage(content=text)


def _ai(tools: list[str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": f"c{i}"} for i, name in enumerate(tools)],
    )


def _tool(name: str = "read_file") -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id="c0", name=name)


def _reads(n: int, *, per_turn: int = 3) -> list[Any]:
    """构造 n 次只读调用，分散在多个 AIMessage 里。

    必须以 **AIMessage 结尾**——``aafter_model`` 看到的最后一条是模型刚返回的
    AIMessage；若以 ToolMessage 结尾，中间件会按「非 AI 结尾」直接跳过。
    """
    out: list[Any] = []
    remaining = n
    while remaining > 0:
        batch = min(per_turn, remaining)
        out.append(_ai(["read_file"] * batch))
        remaining -= batch
        if remaining > 0:
            out.append(_tool("read_file"))
    return out


def _run(mw: ExploreBudgetMiddleware, messages: list[Any]) -> dict[str, Any] | None:
    return asyncio.run(mw.aafter_model({"messages": messages}, None))  # type: ignore[arg-type]


def test_does_nothing_below_budget() -> None:
    mw = ExploreBudgetMiddleware(read_budget=12)
    msgs = [_human(), *_reads(5)]
    assert _run(mw, msgs) is None


def test_nudges_when_budget_reached() -> None:
    mw = ExploreBudgetMiddleware(read_budget=12)
    msgs = [_human(), *_reads(12)]
    result = _run(mw, msgs)
    assert result is not None
    assert result["jump_to"] == "model"
    injected = result["messages"][0]
    assert injected.additional_kwargs.get(_EXPLORE_BUDGET_KEY) is True
    # 提示里要给出计数，让模型知道「已经读了很多」
    assert "12" in injected.content


def test_does_not_nudge_after_a_write() -> None:
    """已经写过文件就不再催——否则会把正在正常推进的任务打断。

    注意必须以 **AIMessage** 结尾：若以 ToolMessage 结尾，中间件会先因
    「最后一条不是 AI」而返回 None，这条测试就**测不到 wrote 分支**
    （2026-11-07 变异检验发现的测试漏洞：删掉 wrote 检查后此测试仍是绿的）。
    """
    mw = ExploreBudgetMiddleware(read_budget=12)
    msgs = [_human(), *_reads(20), _ai(["search_replace"])]
    assert _run(mw, msgs) is None


def test_nudge_stops_after_max_nudges() -> None:
    mw = ExploreBudgetMiddleware(read_budget=4, max_nudges=1)
    msgs = [_human(), *_reads(5)]
    first = _run(mw, msgs)
    assert first is not None

    # 把注入的提醒放进消息历史，再来一次应停止
    msgs2 = [*msgs, first["messages"][0], *_reads(5)]
    assert _run(mw, msgs2) is None


def test_disabled_when_max_nudges_zero() -> None:
    mw = ExploreBudgetMiddleware(read_budget=1, max_nudges=0)
    assert _run(mw, [_human(), *_reads(30)]) is None


def test_counts_only_the_current_turn() -> None:
    """上一轮（更早的真人消息之前）的只读不能算进本轮预算。"""
    mw = ExploreBudgetMiddleware(read_budget=10)
    old_turn = [_human("earlier task"), *_reads(30)]
    new_turn = [_human("new task"), *_reads(3)]
    assert _run(mw, [*old_turn, *new_turn]) is None


def test_execute_command_is_not_counted_as_explore() -> None:
    """跑命令不算「只读探索」——它可能是构建/测试，属于正常推进。"""
    mw = ExploreBudgetMiddleware(read_budget=5)
    msgs = [_human()]
    for _ in range(10):
        msgs.extend([_ai(["execute_command_tool"]), _tool("execute_command_tool")])
    assert _run(mw, msgs) is None


def test_ignores_non_ai_tail() -> None:
    """最后一条不是 AIMessage 时不动作（与 CompletionGuard 同口径）。"""
    mw = ExploreBudgetMiddleware(read_budget=3)
    msgs = [_human(), *_reads(10), _tool()]
    assert _run(mw, msgs) is None


def test_prompt_tells_the_model_to_act() -> None:
    """提示必须明确要求「现在就改」，而不是泛泛的鼓励。"""
    mw = ExploreBudgetMiddleware(read_budget=2)
    result = _run(mw, [_human(), *_reads(3)])
    text = result["messages"][0].content
    assert "修改" in text
    assert "不要再来一轮大范围搜索" in text


def test_profile_default_is_off() -> None:
    """默认关闭——这是行为改变，必须由画像显式开启。"""
    from dataclasses import fields

    from sayacode.config import Profile

    field = next(f for f in fields(Profile) if f.name == "explore_read_budget")
    assert field.default is None


@pytest.mark.parametrize("budget", [1, 12, 30])
def test_budget_is_configurable(budget: int) -> None:
    mw = ExploreBudgetMiddleware(read_budget=budget)
    assert mw.read_budget == budget
    assert _run(mw, [_human(), *_reads(budget - 1)]) is None
    assert _run(mw, [_human(), *_reads(budget)]) is not None
