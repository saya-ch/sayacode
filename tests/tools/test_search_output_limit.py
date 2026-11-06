"""回归：检索输出必须先按 20k 字符截断。

真实问题：3.2.7 加了 ``MAX_SEARCH_OUTPUT_CHARS = 20_000`` 和「请缩小范围」提示语，
但运行时走的是 ``_limited``（按 ``output_limit_bytes`` 默认 64KB 判定），
20k 上限从未生效、提示语只在单测里执行。实测 ``grep_search`` 单次曾返回
125,622 字符（约 3–4 万 token），一次吃掉大半上下文。

2026-11-07 复盘把它列为产品存活缺陷（R4 §2）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sayacode.tools.search import MAX_SEARCH_OUTPUT_CHARS, _bounded_output

BIG = "x" * (MAX_SEARCH_OUTPUT_CHARS + 5_000)


def _runtime() -> Any:
    """最小 ToolRuntime 替身：只需要 ``context`` 供 ``context_value`` 读取。"""
    return SimpleNamespace(context={})


def test_large_output_is_truncated_to_20k_with_hint() -> None:
    result = _bounded_output(BIG, _runtime(), "grep_search")
    text = result if isinstance(result, str) else str(result.get("preview", ""))
    assert len(text) < len(BIG)
    assert "输出已截断" in text
    assert "缩小范围" in text


def test_large_output_truncated_even_without_runtime() -> None:
    """单测路径（runtime=None）也必须截断。"""
    result = _bounded_output(BIG, None, "glob_search")
    assert isinstance(result, str)
    assert len(result) < len(BIG)
    assert "输出已截断" in result


def test_small_output_passes_through() -> None:
    small = "src/a.py:1:import os\nsrc/b.py:2:import sys"
    assert _bounded_output(small, _runtime(), "grep_search") == small
    assert _bounded_output(small, None, "grep_search") == small


def test_hint_mentions_original_size() -> None:
    """提示语要给出原始规模，模型才知道该缩小多少。"""
    result = _bounded_output(BIG, None, "grep_search")
    assert isinstance(result, str)
    assert f"{len(BIG):,}" in result
