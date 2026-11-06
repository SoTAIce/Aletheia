"""merge_intervals 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    f = mod.merge_intervals
    cases = {
        'basic': ([(1, 3), (2, 6), (8, 10), (15, 18)], [(1, 6), (8, 10), (15, 18)]),
        'unsorted lists': ([[8, 10], [1, 3], [2, 6]], [(1, 6), (8, 10)]),
        'touching merge': ([(1, 4), (4, 5)], [(1, 5)]),
        'adjacent kept apart': ([(3, 4), (1, 2)], [(1, 2), (3, 4)]),
        'nested': ([(1, 10), (2, 3), (4, 5)], [(1, 10)]),
        'chained': ([(3, 4), (1, 2), (2, 3)], [(1, 4)]),
        'empty': ([], []),
        'single point': ([(5, 5)], [(5, 5)]),
        'point on edge': ([(1, 3), (3, 3)], [(1, 3)]),
        'negatives': ([(-2, 3), (-5, -1)], [(-5, 3)]),
        'duplicates': ([(1, 2)] * 5, [(1, 2)]),
        'tuple input': (((1, 2), (0, 1)), [(0, 2)]),
    }
    for name, (given, expected) in cases.items():
        yield name, lambda g=given, e=expected: _same(f(g), e)

    def keeps_input():
        data = [[5, 6], [1, 3], [2, 4]]
        f(data)
        return data == [[5, 6], [1, 3], [2, 4]]
    yield 'input not mutated', keeps_input
    yield 'rejects start > end', lambda: _raises(lambda: f([(3, 1)]), ValueError)
    yield 'rejects triple', lambda: _raises(lambda: f([(1, 2, 3)]), ValueError)
    yield 'rejects scalar element', lambda: _raises(lambda: f([5]), ValueError)
    yield 'rejects float end', lambda: _raises(lambda: f([(1, 2.5)]), TypeError)
    yield 'rejects bool end', lambda: _raises(lambda: f([(True, 2)]), TypeError)
    yield 'rejects None', lambda: _raises(lambda: f(None), TypeError)
    yield 'rejects string', lambda: _raises(lambda: f('1-2'), TypeError)


def _same(got, expected):
    return isinstance(got, list) and got == expected and all(type(x) is tuple for x in got)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
