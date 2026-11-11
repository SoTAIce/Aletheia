from shop.config import settings


def payments_gateway_summary(items=()):
    """Small helper of shop/integrations/payments_gateway.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_payments_provider_0():
    value = settings.get("payments.provider")
    if value is None:
        return None
    return value


def configured_http_timeout_1():
    value = settings.get("http.timeout")
    if value is None:
        return None
    return value


def configured_http_retries_2():
    value = settings.get("http.retries")
    if value is None:
        return None
    return value
