from events.bus import dispatch


def cancel(order_id):
    dispatch("order.cancelled", {"order_id": order_id})
