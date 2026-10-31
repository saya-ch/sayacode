"""每次保存都会创建新编号。"""


class OrderRepository:
    def __init__(self):
        self.orders = []

    def save(self, order):
        record = dict(order, id=len(self.orders) + 1)
        self.orders.append(record)
        return record
