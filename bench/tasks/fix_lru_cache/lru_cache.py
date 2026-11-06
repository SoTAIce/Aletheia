from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity):
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError('capacity must be int')
        if capacity < 0:
            raise ValueError('capacity must be at least 1')
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key, default=None):
        return self._data.get(key, default)

    def peek(self, key, default=None):
        return self._data.get(key, default)

    def put(self, key, value):
        evicted = None
        if len(self._data) >= self.capacity:
            evicted, _ = self._data.popitem(last=False)
        self._data[key] = value
        return evicted

    def pop(self, key):
        return self._data.pop(key)

    def size(self):
        return len(self._data)

    def keys(self):
        return list(self._data)
