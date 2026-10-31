"""库存 CSV 读取。"""
import csv
from pathlib import Path


def load_items(path):
    result = []
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            count = int(row["count"] or "1")
            if not count:
                count = 1
            result.append({"sku": row["sku"].strip(), "count": count})
    return result
