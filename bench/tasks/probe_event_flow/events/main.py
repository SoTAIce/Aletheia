import events.handlers  # noqa: F401  registers the handlers
from events.bus import dispatch


def pay(order_id):
    dispatch("order.paid", {"order_id": order_id})
