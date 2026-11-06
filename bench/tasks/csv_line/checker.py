"""parse_csv_line 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    f = mod.parse_csv_line
    cases = [
        (('a,b,c',), ['a', 'b', 'c']),
        (('',), ['']),
        (('a,,c',), ['a', '', 'c']),
        (('a,b,',), ['a', 'b', '']),
        ((',',), ['', '']),
        ((' a , b ',), [' a ', ' b ']),
        (('a,"b,c",d',), ['a', 'b,c', 'd']),
        (('"he said ""hi""",x',), ['he said "hi"', 'x']),
        (('""',), ['']),
        (('"",""',), ['', '']),
        (('""""',), ['"']),
        (('"l1\nl2\r",z',), ['l1\nl2\r', 'z']),
        (('a,"x"',), ['a', 'x']),
        (('a;"b;c";d', ';'), ['a', 'b;c', 'd']),
        (('a\tb', '\t'), ['a', 'b']),
        (('a;b',), ['a;b']),
        (('中,文',), ['中', '文']),
        (('"a,b"', '|'), ['a,b']),
    ]
    for args, expected in cases:
        yield f'parses {args!r}', lambda a=args, e=expected: f(*a) == e
    for text in ['"abc', '"a"b,c', '"a" ,b', 'a"b', 'a\nb', 'x,\r', '"a""', 'a,"b']:
        yield f'rejects {text!r}', lambda t=text: _raises(lambda: f(t), ValueError)
    for sep in ['', ';;', '"', '\n']:
        yield f'rejects sep {sep!r}', lambda s=sep: _raises(lambda: f('a', s), ValueError)
    yield 'rejects None line', lambda: _raises(lambda: f(None), TypeError)
    yield 'rejects int sep', lambda: _raises(lambda: f('a', 1), TypeError)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
