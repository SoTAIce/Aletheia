from events.bus import emit, on


@on("day.closed")
def daily_totals(payload):
    return "daily_totals"
