from dataclasses import dataclass


@dataclass
class Order:
    order_id: int
    total: float
    status: str = "created"   # moves to "paid" when order.paid is dispatched
