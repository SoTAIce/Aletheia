from shop.config import settings


def cli_summary(items=()):
    """Small helper of shop/cli.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_db_host_0():
    value = settings.get("db.host")
    if value is None:
        return None
    return value
