"""订单编排。"""
from pricing import calculate_total


class OrderService:
    def __init__(self, repository, gateway):
        self.repository = repository
        self.gateway = gateway

    def create(self, items):
        total = calculate_total(items)
        order = self.repository.save({"items": items, "total": total})
        self.gateway.charge(order["id"], total)
        return order
