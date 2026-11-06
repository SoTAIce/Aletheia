def _positive_int(value):
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


class TokenBucket:
    def __init__(self, capacity, rate, now=0.0):
        if not _positive_int(capacity):
            raise ValueError('capacity must be a positive int')
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not rate > 0:
            raise ValueError('rate must be positive')
        self.capacity = capacity
        self.rate = rate
        self._tokens = float(capacity)
        self._last = now

    def _refill(self, now):
        self._tokens = self._tokens + (now - self._last) * self.rate
        self._last = now

    def allow(self, now, cost=1):
        if not _positive_int(cost) or cost > self.capacity:
            raise ValueError('cost must be a positive int within capacity')
        self._refill(now)
        allowed = self._tokens > cost
        self._tokens -= cost
        return allowed

    def available(self, now):
        self._refill(now)
        return self._tokens
