def merge_intervals(intervals):
    if not isinstance(intervals, (list, tuple)):
        raise TypeError('intervals must be a list or tuple')
    items = []
    for item in intervals:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError('each interval must be a pair')
        start, end = item
        for value in (start, end):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError('interval ends must be int')
        if start > end:
            raise ValueError('start must not exceed end')
        items.append((start, end))
    items.sort()
    merged = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
