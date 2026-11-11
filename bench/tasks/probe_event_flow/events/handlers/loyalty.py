from events.bus import emit, on


@on("order.paid")
def award_points(payload):
    emit("points.awarded", payload)
    return None
