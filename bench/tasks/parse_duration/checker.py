"""Hidden checks for parse_duration; never copied into the agent's filesystem root."""


def checks(mod):
    f = mod.parse_duration
    ok = {'1h30m': 5400, '45s': 45, '2h': 7200, '0s': 0, ' 1h 5m 3s ': 3903,
          '10m': 600, '1h3s': 3603, '100s': 100}
    for text, expected in ok.items():
        yield f'valid {text!r}', lambda t=text, e=expected: f(t) == e
    for text in ['', '   ', 'h', '10', '-1s', '1.5h', '30m1h', '1h1h', '1H', '1d', '1 h', '1h 2x']:
        yield f'rejects {text!r}', lambda t=text: _raises(f, t, ValueError)
    yield 'rejects non-str', lambda: _raises(f, 5, TypeError)


def _raises(func, arg, error):
    try:
        func(arg)
    except error:
        return True
    except Exception:
        return False
    return False
