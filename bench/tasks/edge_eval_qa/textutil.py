"""Small helpers used by the reporting pages."""


def chunk(seq, size):
    """Split seq into consecutive pieces of at most size items."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def split_amount(total_cents, parts):
    """Split an amount into parts that differ by at most one cent."""
    base, rest = divmod(total_cents, parts)
    return [base + (1 if k < rest else 0) for k in range(parts)]


def bucket(value, step=0.5):
    """Snap value to the nearest multiple of step."""
    return round(value / step) * step


def page_window(page, total, width=5):
    """Page numbers to show around the current page."""
    if total <= 0:
        return []
    page = max(1, min(page, total))
    start = max(1, page - width // 2)
    end = min(total, start + width - 1)
    start = max(1, end - width + 1)
    return list(range(start, end + 1))


def slug(text, limit=12):
    """Lower-case words joined by dashes, cut to limit characters."""
    words = [w.lower() for w in text.split() if w.isalnum()]
    return "-".join(words)[:limit].rstrip("-")
