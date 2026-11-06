"""evaluate 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    f = mod.evaluate
    cases = {
        '1+2*3': 7, '(1+2)*3': 9, '10-4-3': 3, '100/10/5': 2, ' 1 + 2 ': 3, '\t7\n': 7,
        '007': 7, '7/2': 3, '-7/2': -3, '7/-2': -3, '-7/-2': 3, '2^3^2': 512,
        '-2^2': -4, '(-2)^2': 4, '2^0': 1, '0^0': 1, '--3': 3, '+5': 5, '2*-3': -6,
        '2^-0': 1, '((4))': 4, '2^(1+2)*3': 24, '1-(2-3)': 2, '3-2^2*2': -5,
        '12345678901234567890*10': 123456789012345678900,
    }
    for text, expected in cases.items():
        yield f'evaluates {text!r}', lambda t=text, e=expected: _int(f(t)) == e
    for text in ['', '   ', '1+', '(1', '1)', '()', '1 2', '1.5', 'a', '2**3', '*3', '1+*2',
                 '１', '2^', '(1)(2)', '2^-1']:
        yield f'rejects {text!r}', lambda t=text: _raises(lambda: f(t), ValueError)
    yield 'division by zero', lambda: _raises(lambda: f('1/0'), ZeroDivisionError)
    yield 'division by zero expression', lambda: _raises(lambda: f('5/(3-3)'), ZeroDivisionError)
    yield 'rejects None', lambda: _raises(lambda: f(None), TypeError)
    yield 'rejects int', lambda: _raises(lambda: f(3), TypeError)


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
