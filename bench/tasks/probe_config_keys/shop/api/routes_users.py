from shop.config import settings


def routes_users_summary(items=()):
    """Small helper of shop/api/routes_users.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_db_host_0():
    value = settings.get("db.host")
    if value is None:
        return None
    return value
