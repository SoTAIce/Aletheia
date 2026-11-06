import re

_DATE = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})')
_BEFORE_MONTH = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
_MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _ordinal(text):
    """Day number with 0001-01-01 as 1 (a Monday)."""
    if not isinstance(text, str):
        raise TypeError('date must be str')
    match = _DATE.fullmatch(text)
    if not match:
        raise ValueError(f'invalid date: {text!r}')
    year, month, day = (int(part) for part in match.groups())
    if year < 1 or not 1 <= month <= 12:
        raise ValueError(f'invalid date: {text!r}')
    limit = 29 if month == 2 and _leap(year) else _MONTH_DAYS[month - 1]
    if not 1 <= day <= limit:
        raise ValueError(f'invalid date: {text!r}')
    prev = year - 1
    days = prev * 365 + prev // 4 - prev // 100 + prev // 400
    days += _BEFORE_MONTH[month - 1] + (1 if month > 2 and _leap(year) else 0)
    return days + day


def _weekdays_upto(n):
    """Weekdays among day numbers 1..n."""
    return n // 7 * 5 + min(n % 7, 5)


def count_business_days(start, end, holidays=()):
    if not isinstance(holidays, (list, tuple, set, frozenset)):
        raise TypeError('holidays must be a list, tuple or set')
    first, last = _ordinal(start), _ordinal(end)
    days_off = {_ordinal(h) for h in holidays}
    if first > last:
        raise ValueError('start is after end')
    total = _weekdays_upto(last) - _weekdays_upto(first - 1)
    total -= sum(1 for d in days_off if first <= d <= last and (d - 1) % 7 < 5)
    return total
