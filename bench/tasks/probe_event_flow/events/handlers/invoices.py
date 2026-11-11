from events.bus import emit, on


@on("order.paid")
def create_invoice(payload):
    emit("invoice.created", payload)
    return None


@on("order.cancelled")
def void_invoice(payload):
    return "void_invoice"
