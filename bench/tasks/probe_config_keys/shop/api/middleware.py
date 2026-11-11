from shop.config import settings


def middleware_summary(items=()):
    """Small helper of shop/api/middleware.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_log_level_0():
    value = settings.get("log.level")
    if value is None:
        return None
    return value
