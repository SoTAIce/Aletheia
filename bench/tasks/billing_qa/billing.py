"""Invoice helpers used by the billing service."""

MAX_RETRIES = 3
GRACE_DAYS = 5
DAILY_RATE = 0.005      # share of the amount charged per day after the grace period
FEE_CAP_RATIO = 0.1     # the late fee never exceeds this share of the amount


def late_fee(amount, days_overdue):
    """Late fee for an invoice: DAILY_RATE of the amount for every day beyond GRACE_DAYS,
    capped at FEE_CAP_RATIO of the amount and rounded to cents."""
    billable_days = max(0, days_overdue - GRACE_DAYS)
    return round(min(amount * DAILY_RATE * billable_days, amount * FEE_CAP_RATIO), 2)


def retry_delays():
    """Seconds to wait before each payment retry (exponential backoff)."""
    return [2 ** attempt for attempt in range(MAX_RETRIES)]
