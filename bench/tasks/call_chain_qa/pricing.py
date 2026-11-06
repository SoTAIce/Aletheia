"""Price calculation."""

BULK_THRESHOLD = 3


def total_price(items):
    subtotal = sum(item["price"] * item["qty"] for item in items)
    if len(items) >= BULK_THRESHOLD:
        subtotal = apply_discount(subtotal)
    return round_cents(subtotal)


def apply_discount(amount):
    return amount * 0.9


def round_cents(amount):
    return round(amount, 2)


def legacy_total(items):
    return round_cents(sum(item["price"] for item in items))
