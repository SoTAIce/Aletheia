from shop.config import settings


def checkout_summary(items=()):
    """Small helper of shop/core/checkout.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_feature_new_checkout_0():
    value = settings.get("feature.new_checkout")
    if value is None:
        return None
    return value


def configured_payments_provider_1():
    value = settings.get("payments.provider")
    if value is None:
        return None
    return value
