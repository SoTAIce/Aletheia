"""Hidden checks for the fixed inventory.py; never copied into the agent's root."""


def checks(mod):
    Inv = mod.Inventory

    def fresh(**items):
        inv = Inv()
        for sku, qty in items.items():
            inv.add(sku, qty)
        return inv

    yield 'add accumulates', lambda: (lambda i: (i.add('a', 2), i.add('a', 3), i.count('a'))[-1] == 5)(Inv())
    yield 'add rejects zero', lambda: _raises(lambda: Inv().add('a', 0), ValueError)
    yield 'add rejects negative', lambda: _raises(lambda: Inv().add('a', -1), ValueError)
    yield 'count missing is 0', lambda: Inv().count('nope') == 0
    yield 'remove decreases', lambda: (lambda i: (i.remove('a', 2), i.count('a'))[-1] == 3)(fresh(a=5))
    yield 'remove to zero deletes', lambda: (lambda i: (i.remove('a', 5), i.page(1, 10))[-1] == [])(fresh(a=5))

    def insufficient_keeps_stock():
        inv = fresh(a=2)
        return _raises(lambda: inv.remove('a', 3), ValueError) and inv.count('a') == 2
    yield 'insufficient keeps stock', insufficient_keeps_stock

    def missing_sku_unchanged():
        inv = fresh(a=2)
        return _raises(lambda: inv.remove('zz', 1), ValueError) and inv.page(1, 10) == ['a']
    yield 'remove missing sku', missing_sku_unchanged
    yield 'remove rejects zero', lambda: _raises(lambda: fresh(a=1).remove('a', 0), ValueError)
    yield 'page 1 first items', lambda: fresh(c=1, a=1, b=1).page(1, 2) == ['a', 'b']
    yield 'page 2', lambda: fresh(c=1, a=1, b=1).page(2, 2) == ['c']
    yield 'page out of range', lambda: fresh(a=1).page(5, 2) == []
    yield 'page rejects 0', lambda: _raises(lambda: fresh(a=1).page(0, 2), ValueError)
    yield 'size rejects 0', lambda: _raises(lambda: fresh(a=1).page(1, 0), ValueError)
    yield 'total value rounds', lambda: fresh(a=3, b=1).total_value({'a': 0.1, 'b': 1.25}) == 1.55
    yield 'total value missing price', lambda: _raises(lambda: fresh(a=1).total_value({}), KeyError)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
