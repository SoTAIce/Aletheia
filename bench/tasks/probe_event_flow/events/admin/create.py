from events.bus import dispatch


def create(order_id):
    dispatch("order.created", {"order_id": order_id})
