"""订单 HTTP 适配，不依赖外部 Web 框架。"""
from service import OrderService


def create_order(payload, service: OrderService):
    order = service.create(payload["items"])
    return {"id": order["id"], "total": order["total"]}
