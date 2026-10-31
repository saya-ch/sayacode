"""订单计价。"""
from decimal import Decimal


def calculate_total(items):
    return sum((Decimal(str(item["price"])) * item["quantity"] for item in items), Decimal("0"))
