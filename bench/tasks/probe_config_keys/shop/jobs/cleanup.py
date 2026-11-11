from shop.config import settings


def cleanup_summary(items=()):
    """Small helper of shop/jobs/cleanup.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_cache_ttl_0():
    value = settings.get("cache.ttl")
    if value is None:
        return None
    return value
