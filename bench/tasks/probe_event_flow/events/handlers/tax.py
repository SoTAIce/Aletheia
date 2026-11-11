from events.bus import emit, on


@on("invoice.created")
def compute_tax(payload):
    emit("tax.computed", payload)
    return None
