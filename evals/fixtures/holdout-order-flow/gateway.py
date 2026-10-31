"""外部扣款网关。"""


class Gateway:
    def __init__(self, transport):
        self.transport = transport

    def charge(self, order_id, total):
        return self.transport.post("/charges", {"order_id": order_id, "amount": str(total)})
