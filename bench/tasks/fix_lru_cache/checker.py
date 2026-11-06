"""修复后 lru_cache.py 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    C = mod.LRUCache

    def filled(capacity, *keys):
        cache = C(capacity)
        for k in keys:
            cache.put(k, k.upper())
        return cache

    yield 'put then get', lambda: filled(2, 'a').get('a') == 'A'
    yield 'missing returns None', lambda: C(2).get('x') is None
    yield 'missing returns default', lambda: C(2).get('x', 7) == 7

    def evicts_oldest():
        cache = filled(2, 'a', 'b')
        return cache.put('c', 'C') == 'a' and cache.keys() == ['b', 'c']
    yield 'evicts least recent', evicts_oldest
    yield 'no eviction below capacity', lambda: C(3).put('a', 1) is None

    def get_refreshes():
        cache = filled(2, 'a', 'b')
        cache.get('a')
        return cache.put('c', 'C') == 'b' and cache.keys() == ['a', 'c']
    yield 'get refreshes', get_refreshes

    def missing_get_keeps_order():
        cache = filled(2, 'a', 'b')
        cache.get('zz')
        return cache.keys() == ['a', 'b']
    yield 'missing get keeps order', missing_get_keeps_order

    def peek_keeps_order():
        cache = filled(2, 'a', 'b')
        return cache.peek('a') == 'A' and cache.put('c', 'C') == 'a'
    yield 'peek keeps order', peek_keeps_order

    def update_at_full():
        cache = filled(2, 'a', 'b')
        return cache.put('a', 9) is None and cache.size() == 2 and cache.peek('a') == 9 and cache.peek('b') == 'B'
    yield 'update at full keeps all', update_at_full

    def update_refreshes():
        cache = filled(2, 'a', 'b')
        cache.put('a', 9)
        return cache.keys() == ['b', 'a'] and cache.put('c', 'C') == 'b'
    yield 'update refreshes', update_refreshes

    def update_below_capacity():
        cache = filled(3, 'a', 'b')
        cache.put('a', 9)
        return cache.keys() == ['b', 'a'] and cache.put('c', 'C') is None and cache.put('d', 'D') == 'b'
    yield 'update refreshes below capacity', update_below_capacity

    def stores_none():
        cache = C(2)
        cache.put('a', None)
        return cache.get('a', 'd') is None and cache.size() == 1
    yield 'stores None value', stores_none

    def pop_removes():
        cache = filled(3, 'a', 'b')
        return cache.pop('a') == 'A' and cache.keys() == ['b'] and cache.size() == 1
    yield 'pop removes', pop_removes
    yield 'pop missing raises', lambda: _raises(lambda: C(2).pop('x'), KeyError)

    def capacity_one():
        cache = filled(1, 'a')
        return cache.put('b', 'B') == 'a' and cache.keys() == ['b']
    yield 'capacity one', capacity_one

    def long_sequence():
        cache = C(3)
        for k in 'abcd':
            cache.put(k, k)
        cache.get('b')
        cache.put('c', 'c2')
        cache.put('e', 'e')
        return cache.keys() == ['b', 'c', 'e'] and cache.get('c') == 'c2'
    yield 'mixed sequence', long_sequence
    yield 'capacity zero', lambda: _raises(lambda: C(0), ValueError)
    yield 'capacity negative', lambda: _raises(lambda: C(-1), ValueError)
    yield 'capacity str', lambda: _raises(lambda: C('2'), TypeError)
    yield 'capacity bool', lambda: _raises(lambda: C(True), TypeError)
    yield 'capacity float', lambda: _raises(lambda: C(1.5), TypeError)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
