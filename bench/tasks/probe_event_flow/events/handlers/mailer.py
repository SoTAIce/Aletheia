from events.bus import emit, on


@on("invoice.created")
def send_receipt(payload):
    emit("mail.sent", payload)
    return None


@on("order.cancelled")
def send_cancel_notice(payload):
    emit("mail.sent", payload)
    return None
