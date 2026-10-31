"""按区域汇总。"""
from decimal import Decimal


def summarize(rows):
    totals = {}
    for row in rows:
        region = row["region"]
        totals[region] = totals.get(region, Decimal("0")) + Decimal(row["amount"])
    return totals
