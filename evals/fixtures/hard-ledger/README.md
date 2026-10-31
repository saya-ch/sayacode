# Hard Ledger

小型复式记账库。当前使用浮点数记录金额，并缺少借贷平衡与幂等校验。

## 现有公开 API

- `money.parse_amount(text) -> float`
- `money.format_amount(value) -> str`
- `account.Account(name)`，有 `debits`、`credits`、`balance`
- `ledger.Ledger().post(entry_id, lines)`，`lines` 为 `(账户名, "debit"|"credit", 金额文本)`
- `ledger.Ledger().trial_balance() -> dict`
- `ledger.UnbalancedEntry`

## 运行测试

```
python -m unittest discover -s tests
```
