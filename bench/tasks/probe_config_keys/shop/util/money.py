def money_summary(items=()):
    """Small helper of shop/util/money.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count
