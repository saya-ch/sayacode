"""销售记录读取。"""
import csv
from pathlib import Path


def load(path):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))
