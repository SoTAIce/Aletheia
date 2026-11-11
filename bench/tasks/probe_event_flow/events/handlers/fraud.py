from events.bus import emit, on


@on("order.created")
def check_order(payload):
    emit("fraud.checked", payload)
    return None
