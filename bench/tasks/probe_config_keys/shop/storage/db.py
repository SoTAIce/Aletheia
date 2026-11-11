from shop.config import settings


def db_summary(items=()):
    """Small helper of shop/storage/db.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_db_host_0():
    value = settings.get("db.host")
    if value is None:
        return None
    return value


def configured_db_port_1():
    value = settings.get("db.port")
    if value is None:
        return None
    return value


def configured_db_pool_size_2():
    value = settings.get("db.pool_size")
    if value is None:
        return None
    return value
