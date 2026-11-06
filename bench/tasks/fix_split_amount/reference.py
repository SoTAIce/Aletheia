def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{name} must be int')


def allocate(total_cents, weights):
    _require_int(total_cents, 'total_cents')
    if total_cents < 0:
        raise ValueError('total must not be negative')
    if not isinstance(weights, (list, tuple)):
        raise TypeError('weights must be a list or tuple')
    if not weights:
        raise ValueError('weights must not be empty')
    for w in weights:
        _require_int(w, 'weight')
        if w < 0:
            raise ValueError('weights must not be negative')
    total_weight = sum(weights)
    if total_weight == 0:
        raise ValueError('weights must not all be zero')
    shares = [total_cents * w // total_weight for w in weights]
    left = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(total_cents * weights[i] % total_weight), i))
    for i in order[:left]:
        shares[i] += 1
    return shares


def split_even(total_cents, parts):
    _require_int(parts, 'parts')
    if parts < 1:
        raise ValueError('parts must be at least 1')
    return allocate(total_cents, [1] * parts)
