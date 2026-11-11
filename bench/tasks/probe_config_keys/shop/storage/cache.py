from shop.config import settings


def cache_summary(items=()):
    """Small helper of shop/storage/cache.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_cache_ttl_0():
    value = settings.get("cache.ttl")
    if value is None:
        return None
    return value


def configured_cache_backend_1():
    value = settings.get("cache.backend")
    if value is None:
        return None
    return value
