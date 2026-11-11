from shop.config import settings


def webhooks_summary(items=()):
    """Small helper of shop/integrations/webhooks.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_http_retries_0():
    value = settings.get("http.retries")
    if value is None:
        return None
    return value
