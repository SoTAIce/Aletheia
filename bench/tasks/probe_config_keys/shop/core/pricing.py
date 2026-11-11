from shop.config import settings


def pricing_summary(items=()):
    """Small helper of shop/core/pricing.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_payments_currency_0():
    value = settings.get("payments.currency")
    if value is None:
        return None
    return value
