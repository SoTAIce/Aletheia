from shop.config import settings


def routes_search_summary(items=()):
    """Small helper of shop/api/routes_search.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_search_page_size_0():
    value = settings.get("search.page_size")
    if value is None:
        return None
    return value


def clamp(n, row):
    limit = row.get("search.max_results", 100)
    return min(n, limit)
