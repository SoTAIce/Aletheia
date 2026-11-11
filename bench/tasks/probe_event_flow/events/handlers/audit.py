from events.bus import emit, on


@on("ledger.posted")
def audit_ledger(payload):
    return "audit_ledger"


@on("shipment.booked")
def audit_shipment(payload):
    return "audit_shipment"
