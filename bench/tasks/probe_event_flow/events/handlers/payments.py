from events.bus import emit, on


@on("order.paid")
def record_payment(payload):
    emit("ledger.entry", payload)
    return None


@on("order.refunded")
def refund_payment(payload):
    emit("ledger.entry", payload)
    return None
