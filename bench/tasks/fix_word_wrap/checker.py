"""修复后 word_wrap.py 的隐藏检查，绝不复制到智能体的根目录。"""

PARAGRAPH = ('It was the best of times it was the worst of times it was the age of wisdom '
             'it was the age of foolishness')


def checks(mod):
    f = mod.wrap
    cases = {
        'simple': (('the quick brown fox', 10), ['the quick', 'brown fox']),
        'exact width': (('aaa bbb', 7), ['aaa bbb']),
        'exact width then wrap': (('aa bb cc', 5), ['aa bb', 'cc']),
        'width one': (('a b', 1), ['a', 'b']),
        'collapses spaces': (('a   b', 10), ['a b']),
        'tabs and newlines': (('a\tb\nc', 10), ['a b c']),
        'trims ends': ((' a b ', 10), ['a b']),
        'empty': (('', 5), []),
        'whitespace only': ((' \n\t ', 5), []),
        'long word': (('abcdefghij', 4), ['abcd', 'efgh', 'ij']),
        'long word after text': (('hi abcdefgh', 4), ['hi', 'abcd', 'efgh']),
        'tail joins next word': (('abcdefg hi', 5), ['abcde', 'fg hi']),
        'exact multiple': (('abcdef x', 3), ['abc', 'def', 'x']),
        'unicode': (('你好 世界', 5), ['你好 世界']),
    }
    for name, (args, expected) in cases.items():
        yield name, lambda a=args, e=expected: f(*a) == e

    def paragraph_invariants():
        lines = f(PARAGRAPH, 12)
        return (all(0 < len(line) <= 12 and line == line.strip() for line in lines)
                and ' '.join(lines) == PARAGRAPH
                and all(len(a) + 1 + len(b.split()[0]) > 12 for a, b in zip(lines, lines[1:])))
    yield 'paragraph greedy', paragraph_invariants
    yield 'rejects width zero', lambda: _raises(lambda: f('a', 0), ValueError)
    yield 'rejects negative width', lambda: _raises(lambda: f('a', -3), ValueError)
    yield 'rejects str width', lambda: _raises(lambda: f('a', '5'), TypeError)
    yield 'rejects bool width', lambda: _raises(lambda: f('a', True), TypeError)
    yield 'rejects float width', lambda: _raises(lambda: f('a', 2.0), TypeError)
    yield 'rejects None text', lambda: _raises(lambda: f(None, 5), TypeError)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
