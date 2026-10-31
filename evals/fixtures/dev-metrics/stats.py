"""纯 Python 数值统计。"""
import math


def mean(values):
    return sum(values) / len(values)


def percentile(values, p):
    ordered = sorted(values)
    return ordered[math.floor((len(ordered) - 1) * p / 100)]
