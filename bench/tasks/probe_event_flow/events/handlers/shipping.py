from events.bus import emit, on


@on("shipment.requested")
def book_courier(payload):
    emit("shipment.booked", payload)
    emit("mail.sent", payload)
    return None
