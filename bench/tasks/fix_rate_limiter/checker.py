"""修复后 rate_limiter.py 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    TB = mod.TokenBucket

    def run(bucket, calls):
        return [bucket.allow(*c) for c in calls]

    yield 'fresh bucket drains', lambda: run(TB(3, 1), [(0,), (0,), (0,), (0,)]) == [True, True, True, False]
    yield 'cost equal to tokens', lambda: TB(2, 1).allow(0, 2) is True
    yield 'capacity one allows once', lambda: run(TB(1, 1), [(0,), (0,)]) == [True, False]
    yield 'refills over time', lambda: run(TB(2, 1), [(0, 2), (0.5,), (1.0,)]) == [True, False, True]
    yield 'fractional rate', lambda: run(TB(1, 0.25), [(0,), (3,), (4,)]) == [True, False, True]
    yield 'refill capped', lambda: TB(2, 1).available(100) == 2.0

    def capped_then_drains():
        bucket = TB(2, 1)
        return run(bucket, [(100, 2), (100,)]) == [True, False]
    yield 'cap limits burst', capped_then_drains
    yield 'deny keeps tokens', lambda: run(TB(1, 1), [(0,), (0.5,), (1.0,)]) == [True, False, True]

    def available_after_deny():
        bucket = TB(2, 0.5)
        return run(bucket, [(0, 2), (1,)]) == [True, False] and bucket.available(1) == 0.5
    yield 'available after deny', available_after_deny
    yield 'available does not consume', lambda: (lambda b: (b.available(0), b.available(0))[-1] == 3.0)(TB(3, 1))
    yield 'start time honoured', lambda: run(TB(1, 1, now=100), [(100,), (100.5,), (101,)]) == [True, False, True]
    yield 'same timestamp ok', lambda: run(TB(2, 1, now=5), [(5,), (5,)]) == [True, True]
    yield 'backwards allow raises', lambda: _raises(lambda: TB(1, 1, now=10).allow(5), ValueError)
    yield 'backwards available raises', lambda: _raises(lambda: TB(1, 1, now=10).available(9.5), ValueError)

    def backwards_keeps_state():
        bucket = TB(2, 1, now=10)
        bucket.allow(10, 2)
        return _raises(lambda: bucket.allow(5), ValueError) and bucket.available(10) == 0.0 and bucket.allow(11) is True
    yield 'backwards keeps state', backwards_keeps_state
    yield 'cost over capacity', lambda: _raises(lambda: TB(2, 1).allow(0, 3), ValueError)
    yield 'cost zero', lambda: _raises(lambda: TB(2, 1).allow(0, 0), ValueError)
    yield 'cost bool', lambda: _raises(lambda: TB(2, 1).allow(0, True), ValueError)
    yield 'cost float', lambda: _raises(lambda: TB(2, 1).allow(0, 1.5), ValueError)
    yield 'capacity zero', lambda: _raises(lambda: TB(0, 1), ValueError)
    yield 'capacity bool', lambda: _raises(lambda: TB(True, 1), ValueError)
    yield 'capacity float', lambda: _raises(lambda: TB(2.5, 1), ValueError)
    yield 'rate zero', lambda: _raises(lambda: TB(1, 0), ValueError)
    yield 'rate negative', lambda: _raises(lambda: TB(1, -1), ValueError)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
