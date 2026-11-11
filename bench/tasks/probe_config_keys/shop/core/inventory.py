from shop.config import settings


def inventory_summary(items=()):
    """Small helper of shop/core/inventory.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_db_pool_size_0():
    value = settings.get("db.pool_size")
    if value is None:
        return None
    return value
