from shop.config import settings


def worker_summary(items=()):
    """Small helper of shop/jobs/worker.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


def configured_jobs_concurrency_0():
    value = settings.get("jobs.concurrency")
    if value is None:
        return None
    return value


def configured_jobs_poll_seconds_1():
    value = settings.get("jobs.poll_seconds")
    if value is None:
        return None
    return value


def configured_jobs_max_attempts_2():
    value = settings.get("jobs.max_attempts")
    if value is None:
        return None
    return value
