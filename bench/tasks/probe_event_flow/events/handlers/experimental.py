from events.bus import emit, on


@on("shipment.booked")
def notify_partner(payload):
    emit("partner.notified", payload)
    return None
