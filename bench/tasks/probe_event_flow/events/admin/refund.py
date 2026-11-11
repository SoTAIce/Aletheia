from events.bus import dispatch


def refund(order_id):
    # emit("order.refunded") used to be called here directly; it now goes through dispatch.
    dispatch("order.refunded", {"order_id": order_id})
