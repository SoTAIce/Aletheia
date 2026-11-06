"""compare_versions 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    f = mod.compare_versions
    cases = [
        ('1.0.0', '2.0.0', -1),
        ('2.0.0', '1.9.9', 1),
        ('1.10.0', '1.9.0', 1),
        ('1.2.3', '1.2.10', -1),
        ('1.2.3', '1.2.3', 0),
        ('0.0.0', '0.0.0', 0),
        ('1.0.0+build.1', '1.0.0+zzz', 0),
        ('1.0.0+001', '1.0.0', 0),
        ('1.0.0-rc.1+b', '1.0.0-rc.1', 0),
        ('1.0.0-alpha', '1.0.0', -1),
        ('1.0.0', '1.0.0-rc.1', 1),
        ('1.0.0-0a', '1.0.0-1', 1),
        ('1.0.0-x-y', '1.0.0-x', 1),
        ('1.0.0-A', '1.0.0-a', -1),
        ('2.0.0-alpha', '1.9.9', 1),
    ]
    for a, b, expected in cases:
        yield f'{a} vs {b}', lambda a=a, b=b, e=expected: _int(f(a, b)) == e
    chain = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta',
             '1.0.0-beta.2', '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0']
    for low, high in zip(chain, chain[1:]):
        yield f'chain {low} < {high}', lambda lo=low, hi=high: _int(f(lo, hi)) == -1 and _int(f(hi, lo)) == 1
    bad = ['1.0', '1.0.0.0', '01.0.0', '1.00.0', '1.0.0-', '1.0.0-01', '1.0.0-a..b',
           'v1.0.0', ' 1.0.0', '1.0.0\n', '1.0.0+', '1.0.0-a$', '', '1.0.0-beta+x+y', '-1.0.0']
    for text in bad:
        yield f'rejects {text!r}', lambda t=text: (_raises(lambda: f(t, '1.0.0'), ValueError)
                                                   and _raises(lambda: f('1.0.0', t), ValueError))
    yield 'rejects int', lambda: _raises(lambda: f(1, '1.0.0'), TypeError)
    yield 'rejects None', lambda: _raises(lambda: f('1.0.0', None), TypeError)


def _int(value):
    return value if type(value) is int else None


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
