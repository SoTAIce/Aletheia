from shop.config import settings


def server_summary(items=()):
    """Small helper of shop/api/server.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_http_timeout_0():
    value = settings.get("http.timeout")
    if value is None:
        return None
    return value


def configured_http_user_agent_1():
    value = settings.get("http.user_agent")
    if value is None:
        return None
    return value
