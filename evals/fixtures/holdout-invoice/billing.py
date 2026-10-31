"""金额计算。"""
from decimal import ROUND_HALF_UP, Decimal


def subtotal(invoice):
    total = sum((line.unit_price * line.quantity for line in invoice.lines), Decimal("0"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def with_tax(amount, rate):
    return (amount * (Decimal("1") + rate)).quantize(Decimal("0.01"))
