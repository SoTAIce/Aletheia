from shop.config import settings


def orders_summary(items=()):
    """Small helper of shop/core/orders.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_payments_currency_0():
    value = settings.get("payments.currency")
    if value is None:
        return None
    return value


def configured_feature_new_checkout_1():
    value = settings.get("feature.new_checkout")
    if value is None:
        return None
    return value
