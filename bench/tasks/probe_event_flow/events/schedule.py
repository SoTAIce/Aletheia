from events.bus import emit


def close_day():
    """Called by the nightly scheduler, never by main.pay."""
    emit("day.closed")


def remind_unpaid():
    emit("order.reminder")
