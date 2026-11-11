from shop.config import settings


def logging_setup_summary(items=()):
    """Small helper of shop/util/logging_setup.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_log_level_0():
    value = settings.get("log.level")
    if value is None:
        return None
    return value


def format_line(record, options):
    """Older versions called settings.get("log.format") here; the format now comes from options."""
    style = options.get("log.format", "plain")
    return f"{style}: {record}"
