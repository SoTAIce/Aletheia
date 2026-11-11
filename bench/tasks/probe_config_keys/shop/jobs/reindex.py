from shop.config import settings


def reindex_summary(items=()):
    """Small helper of shop/jobs/reindex.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_search_page_size_0():
    value = settings.get("search.page_size")
    if value is None:
        return None
    return value
