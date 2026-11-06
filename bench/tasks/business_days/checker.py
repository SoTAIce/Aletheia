"""count_business_days 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    f = mod.count_business_days
    cases = {
        'single monday': (('2024-03-04', '2024-03-04'), 1),
        'weekend only': (('2024-03-09', '2024-03-10'), 0),
        'full week': (('2024-03-04', '2024-03-10'), 5),
        'friday to monday': (('2024-03-08', '2024-03-11'), 2),
        'leap year 2024': (('2024-01-01', '2024-12-31'), 262),
        'common year 2023': (('2023-01-01', '2023-12-31'), 260),
        'across leap day': (('2024-02-26', '2024-03-01'), 5),
        'year 2000 leap day': (('2000-02-28', '2000-03-01'), 3),
        'across new year': (('2023-12-29', '2024-01-02'), 3),
        'first week of year 1': (('0001-01-01', '0001-01-07'), 5),
        'decades': (('1999-01-01', '2030-12-31'), 8348),
        'whole calendar': (('0001-01-01', '9999-12-31'), 2608615),
    }
    for name, (args, expected) in cases.items():
        yield name, lambda a=args, e=expected: _int(f(*a)) == e
    week = ('2024-12-23', '2024-12-27')
    yield 'holiday dedup and weekend', lambda: _int(f(*week, ['2024-12-25', '2024-12-25', '2024-12-28'])) == 4
    yield 'holiday outside range', lambda: _int(f(*week, ['2024-12-20', '2025-01-01'])) == 5
    yield 'holiday tuple on edges', lambda: _int(f(*week, ('2024-12-23', '2024-12-27'))) == 3
    yield 'holiday set', lambda: _int(f(*week, {'2024-12-24'})) == 4
    for text in ['2024-13-01', '2024-00-10', '2024-02-30', '2023-02-29', '1900-02-29', '2024-04-31',
                 '2024-2-03', '2024/02/03', '', '0000-01-01', ' 2024-01-01', '2024-01-01\n', '24-01-01']:
        yield f'rejects {text!r}', lambda t=text: (_raises(lambda: f(t, '9999-12-31'), ValueError)
                                                   and _raises(lambda: f('0001-01-01', t), ValueError))
    yield 'accepts 9999-12-31', lambda: _int(f('9999-12-31', '9999-12-31')) == 1
    yield 'rejects bad holiday', lambda: _raises(lambda: f(*week, ['2024-02-30']), ValueError)
    yield 'rejects start after end', lambda: _raises(lambda: f('2024-03-05', '2024-03-04'), ValueError)
    yield 'rejects non-str date', lambda: _raises(lambda: f(20240101, '2024-01-02'), TypeError)
    yield 'rejects non-str holiday', lambda: _raises(lambda: f(*week, [20241225]), TypeError)
    yield 'rejects holidays string', lambda: _raises(lambda: f(*week, '2024-12-25'), TypeError)


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
