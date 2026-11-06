_DIGITS = '0123456789'
_SYMBOLS = '+-*/^()'


def _tokenize(expr):
    tokens = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
        elif c in _DIGITS:
            j = i
            while j < n and expr[j] in _DIGITS:
                j += 1
            tokens.append(int(expr[i:j]))
            i = j
        elif c in _SYMBOLS:
            tokens.append(c)
            i += 1
        else:
            raise ValueError(f'unexpected character {c!r}')
    return tokens


def _divide(a, b):
    if b == 0:
        raise ZeroDivisionError('division by zero')
    quotient = abs(a) // abs(b)
    return -quotient if (a < 0) != (b < 0) else quotient


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        token = self.peek()
        if token is None:
            raise ValueError('unexpected end of expression')
        self.pos += 1
        return token

    def is_op(self, ops):
        token = self.peek()
        return isinstance(token, str) and token in ops

    def expr(self):
        value = self.term()
        while self.is_op('+-'):
            op = self.take()
            right = self.term()
            value = value + right if op == '+' else value - right
        return value

    def term(self):
        value = self.unary()
        while self.is_op('*/'):
            op = self.take()
            right = self.unary()
            value = value * right if op == '*' else _divide(value, right)
        return value

    def unary(self):
        if self.is_op('+-'):
            op = self.take()
            value = self.unary()
            return -value if op == '-' else value
        return self.power()

    def power(self):
        base = self.atom()
        if self.is_op('^'):
            self.take()
            exponent = self.unary()
            if exponent < 0:
                raise ValueError('negative exponent')
            return base ** exponent
        return base

    def atom(self):
        token = self.take()
        if isinstance(token, int):
            return token
        if token == '(':
            value = self.expr()
            if self.take() != ')':
                raise ValueError('expected )')
            return value
        raise ValueError(f'unexpected token {token!r}')


def evaluate(expr):
    if not isinstance(expr, str):
        raise TypeError('expr must be str')
    parser = _Parser(_tokenize(expr))
    value = parser.expr()
    if parser.pos != len(parser.tokens):
        raise ValueError('unexpected trailing input')
    return value
