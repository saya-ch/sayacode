"""账户：累计借贷并给出余额（初始实现使用浮点）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Account:
    name: str
    debits: float = 0.0
    credits: float = 0.0

    @property
    def balance(self) -> float:
        return round(self.debits - self.credits, 10)
