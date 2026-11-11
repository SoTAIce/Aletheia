from events.bus import emit, on


@on("order.created")
def reserve_stock(payload):
    emit("stock.reserved", payload)
    return None


@on("order.paid")
def ship_stock(payload):
    emit("shipment.requested", payload)
    return None
