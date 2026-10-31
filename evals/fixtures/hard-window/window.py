"""滚动统计（初始实现：朴素算法、窗口与分位数定义不正确）。"""

from __future__ import annotations


def rolling_median(values: list[float], k: int) -> list[float | None]:
    result: list[float | None] = []
    for i in range(len(values)):
        window = values[max(0, i - k + 1) : i + 1]
        ordered = sorted(window)
        result.append(ordered[len(ordered) // 2])
    return result


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(len(ordered) * 0.95)]


def top_k_frequent(values: list[int], k: int) -> list[tuple[int, int]]:
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[:k]
