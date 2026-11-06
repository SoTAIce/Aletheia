"""Request entry point."""

from notify import log_event
from service import cancel_order, place_order


def handle(request):
    kind = request.get("kind")
    if kind == "place":
        return place_order(request["user"], request["items"])
    if kind == "cancel":
        return cancel_order(request["order_id"])
    log_event("unknown_request", kind)
    return {"ok": False}
