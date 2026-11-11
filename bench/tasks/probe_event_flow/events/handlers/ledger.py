from events.bus import emit, on


@on("ledger.entry")
def post_entry(payload):
    emit("ledger.posted", payload)
    return None
