"""In-memory order storage."""

_STORE = {1: {"user": "demo", "items": [], "total": 0}}


def _next_id():
    return max(_STORE, default=0) + 1


def _write(order_id, order):
    if order is None:
        _STORE.pop(order_id, None)
    else:
        _STORE[order_id] = order


def save_order(user, items, total):
    order_id = _next_id()
    _write(order_id, {"user": user, "items": items, "total": total})
    return order_id


def load_order(order_id):
    return _STORE.get(order_id)


def delete_order(order_id):
    _write(order_id, None)


def list_orders():
    return sorted(_STORE)
