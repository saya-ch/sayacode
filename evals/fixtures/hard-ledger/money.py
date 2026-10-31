"""金额工具（初始实现使用 float，存在二进制精度问题）。"""

from __future__ import annotations


def parse_amount(text: str) -> float:
    """把带千分位的金额文本转成浮点数。"""
    return float(text.replace(",", ""))


def format_amount(value: float) -> str:
    """把金额格式化为两位小数。"""
    return f"{value:.2f}"
