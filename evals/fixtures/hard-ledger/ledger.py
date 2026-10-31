"""简单复式记账（初始实现不校验平衡、不处理幂等、使用浮点）。"""

from __future__ import annotations

from account import Account
from money import parse_amount


class UnbalancedEntry(Exception):
    """借贷不平衡时抛出。"""


class Ledger:
    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self.entries: list[dict] = []

    def account(self, name: str) -> Account:
        if name not in self.accounts:
            self.accounts[name] = Account(name)
        return self.accounts[name]

    def post(self, entry_id: str, lines: list[tuple[str, str, str]]) -> None:
        """lines 为 (账户名, 'debit'|'credit', 金额文本) 列表。"""
        parsed = [(acc, side, parse_amount(amount)) for acc, side, amount in lines]
        for acc, side, value in parsed:
            account = self.account(acc)
            if side == "debit":
                account.debits += value
            else:
                account.credits += value
        self.entries.append({"id": entry_id, "lines": parsed})

    def trial_balance(self) -> dict[str, float]:
        return {name: account.balance for name, account in self.accounts.items()}
