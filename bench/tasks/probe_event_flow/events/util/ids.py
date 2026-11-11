import itertools

_counter = itertools.count(1)


def next_id():
    return next(_counter)
