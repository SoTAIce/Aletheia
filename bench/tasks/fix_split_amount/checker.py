"""修复后 allocate.py 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    a, even = mod.allocate, mod.split_even
    cases = {
        'thirds': ((100, [1, 1, 1]), [34, 33, 33]),
        'one to two': ((100, [1, 2]), [33, 67]),
        'exact split': ((10, [3, 3, 4]), [3, 3, 4]),
        'zero weight': ((7, [0, 1, 1]), [0, 4, 3]),
        'largest remainder wins': ((10, [1, 1, 5]), [2, 1, 7]),
        'tie lower index': ((2, [1, 1, 1]), [1, 1, 0]),
        'remainder order': ((11, [2, 3, 1, 1]), [3, 5, 2, 1]),
        'zero total': ((0, [1, 2, 3]), [0, 0, 0]),
        'tuple weights': ((5, (1, 1)), [3, 2]),
        'huge amount': ((10 ** 18 + 1, [1, 1]), [500000000000000001, 500000000000000000]),
        'huge weights': ((100, [10 ** 20, 1]), [100, 0]),
    }
    for name, (args, expected) in cases.items():
        yield name, lambda x=args, e=expected: a(*x) == e

    def always_exact():
        for weights in ([1, 2, 3], [5, 0, 7], [1, 1, 1, 1], [9, 1], [3, 3, 3, 1]):
            s = sum(weights)
            for total in range(0, 60):
                shares = a(total, weights)
                floors = [total * w // s for w in weights]
                if sum(shares) != total or any(not f <= x <= f + 1 for f, x in zip(floors, shares)):
                    return False
        return True
    yield 'sum always exact', always_exact
    yield 'split even thirds', lambda: even(100, 3) == [34, 33, 33]
    yield 'split even two', lambda: even(5, 2) == [3, 2]
    yield 'split even one', lambda: even(7, 1) == [7]
    yield 'rejects negative total', lambda: _raises(lambda: a(-1, [1]), ValueError)
    yield 'rejects empty weights', lambda: _raises(lambda: a(10, []), ValueError)
    yield 'rejects zero weights', lambda: _raises(lambda: a(10, [0, 0]), ValueError)
    yield 'rejects negative weight', lambda: _raises(lambda: a(10, [1, -1, 2]), ValueError)
    yield 'rejects float total', lambda: _raises(lambda: a(10.0, [1, 1]), TypeError)
    yield 'rejects bool total', lambda: _raises(lambda: a(True, [1]), TypeError)
    yield 'rejects float weight', lambda: _raises(lambda: a(10, [1.5, 1]), TypeError)
    yield 'rejects string weights', lambda: _raises(lambda: a(10, '11'), TypeError)
    yield 'rejects zero parts', lambda: _raises(lambda: even(10, 0), ValueError)
    yield 'rejects bool parts', lambda: _raises(lambda: even(10, True), TypeError)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
