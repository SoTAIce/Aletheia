"""Order use cases."""

from notify import log_event, send_email
from pricing import total_price
from repo import delete_order, load_order, save_order


def place_order(user, items):
    total = total_price(items)
    order_id = save_order(user, items, total)
    _confirm(user, order_id)
    return {"ok": True, "id": order_id, "total": total}


def _confirm(user, order_id):
    send_email(user, f"order {order_id} placed")


def cancel_order(order_id):
    order = load_order(order_id)
    if order is None:
        return {"ok": False}
    delete_order(order_id)
    log_event("cancelled", order_id)
    return {"ok": True}


def reprice_all(orders):
    return [total_price(o["items"]) for o in orders]
