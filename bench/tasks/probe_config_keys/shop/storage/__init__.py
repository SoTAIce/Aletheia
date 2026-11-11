def __init___summary(items=()):
    """Small helper of shop/storage/__init__.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count
