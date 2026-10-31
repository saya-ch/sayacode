"""发票数据结构。"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Line:
    label: str
    unit_price: Decimal
    quantity: int


class Invoice:
    def __init__(self, lines=[]):
        self.lines = lines

    def add(self, line):
        self.lines.append(line)
