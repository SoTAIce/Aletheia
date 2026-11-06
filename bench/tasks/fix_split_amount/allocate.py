def allocate(total_cents, weights):
    if total_cents < 0:
        raise ValueError('total must not be negative')
    if not weights:
        raise ValueError('weights must not be empty')
    total_weight = sum(weights)
    shares = [total_cents * w // total_weight for w in weights]
    left = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: total_cents * weights[i] % total_weight)
    for i in order[:left]:
        shares[i] += 1
    return shares


def split_even(total_cents, parts):
    if parts < 1:
        raise ValueError('parts must be at least 1')
    return allocate(total_cents, [1] * parts)
