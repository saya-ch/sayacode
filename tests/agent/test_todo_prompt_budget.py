"""回归：todo 提示必须紧凑，但不能丢掉有约束力的内容。

背景（2026-11-07）：TodoList 两段提示原为 6,502 字符，其中 **81% 是 LangChain
框架默认文案**（系统提示 1,370 + 工具描述 3,873），我们只追加了 1,259。
对照业界源码：OpenCode 的 todowrite.txt 仅 512 字符，且为 experimental 默认关闭；
Codex 干脆没有 todo 工具（用 notes）。

压缩到 1,139 字符（-82%），把重复说教删掉，但**所有有行为约束力的条目必须保留**。
本文件锁住这两件事，防止后续又慢慢膨胀回去。
"""

from __future__ import annotations

import pytest

from sayacode.agent.graph import _SAYACODE_PLANNING_PROMPT, _SAYACODE_TODO_DESCRIPTION

#: OpenCode todowrite 的实际大小，作为外部参照
OPENCODE_TODOWRITE_CHARS = 512


@pytest.fixture(scope="module")
def both() -> str:
    return _SAYACODE_PLANNING_PROMPT + _SAYACODE_TODO_DESCRIPTION


def test_total_size_is_compact(both: str) -> None:
    """两段合计不能超过 1,800 字符——每轮请求都要重发。"""
    assert len(both) < 1_800, f"todo 提示 {len(both)} 字符，已超过 1800 上限"


def test_size_is_within_a_few_times_opencode(both: str) -> None:
    """外部参照：不应是 OpenCode todowrite 的很多倍。"""
    ratio = len(both) / OPENCODE_TODOWRITE_CHARS
    assert ratio < 3.5, f"是 OpenCode 的 {ratio:.1f} 倍（{len(both)} 字符）"


def test_keeps_when_to_use_and_skip(both: str) -> None:
    """必须保留「何时用 / 何时跳过」——这是防止小任务被 todo 拖慢的关键。"""
    assert "three or more distinct steps" in both
    assert "Skip it for" in both


def test_keeps_state_transitions(both: str) -> None:
    """状态标记规则必须保留，否则 todo 失去意义。"""
    assert "in_progress" in both
    assert "completed" in both


def test_keeps_no_false_completion_rule(both: str) -> None:
    """不许在未完成时标记完成——这是防虚报的核心约束。"""
    assert "Never mark a step completed while it is partial, blocked or unverified" in both


def test_keeps_no_parallel_call_rule(both: str) -> None:
    """并行调用 todo 工具会互相覆盖列表。"""
    assert "Do not call `write_todos` twice in parallel" in both


def test_keeps_final_answer_rule(both: str) -> None:
    """最终答案必须在最后一次 todo 调用之后——否则用户只看到列表更新没看到结果。"""
    assert "AFTER your last" in both
    assert "not an answer" in both


def test_keeps_delegation_rule(both: str) -> None:
    """子 agent 结果是证据而非自动验收，这条不能丢。"""
    assert "evidence to inspect" in both


def test_does_not_pull_in_framework_defaults(both: str) -> None:
    """不能再把 LangChain 默认文案拼回来——那正是 81% 臃肿的来源。"""
    forbidden = [
        "## When to Use This Tool",
        "## When NOT to Use This Tool",
        "## Task States and Management",
        "Important To-Do List Usage Notes to Remember",
    ]
    for text in forbidden:
        assert text not in both, f"回来了框架默认段落：{text}"


def test_no_repeated_skip_lecture(both: str) -> None:
    """「简单任务不要用」原来说了 5 次，压到不超过 2 次。"""
    assert both.lower().count("skip it") <= 2
