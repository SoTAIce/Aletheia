from shop.config import settings


def routes_orders_summary(items=()):
    """Small helper of shop/api/routes_orders.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_db_host_0():
    value = settings.get("db.host")
    if value is None:
        return None
    return value


def configured_search_page_size_1():
    value = settings.get("search.page_size")
    if value is None:
        return None
    return value
