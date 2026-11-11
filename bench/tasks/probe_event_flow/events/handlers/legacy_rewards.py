from events.bus import emit, on


@on("order.paid")
def double_points(payload):
    emit("points.awarded", payload)
    return None


@on("points.awarded")
def expire_points(payload):
    return "expire_points"
