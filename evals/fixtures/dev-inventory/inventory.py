"""库存与折扣计算。"""


class Inventory:
    def __init__(self, items={}):
        self.items = items

    def add(self, name, count):
        self.items[name] = self.items.get(name, 0) + count

    def count(self, name):
        return self.items.get(name, 0)


def discount(price, rate):
    return price * (1 - rate)
