"""Descriptive statistics for small samples."""


def mean(values):
    return sum(values) / len(values)


def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle] + ordered[middle + 1]) / 2


def mode(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get)


def value_range(values):
    if not values:
        raise ValueError("empty sample")
    return max(values) - min(values)


def normalize(values):
    low, high = min(values), max(values)
    return [(v - low) / (high - low) for v in values]
