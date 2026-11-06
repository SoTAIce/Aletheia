from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity):
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError('capacity must be int')
        if capacity < 1:
            raise ValueError('capacity must be at least 1')
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def peek(self, key, default=None):
        return self._data.get(key, default)

    def put(self, key, value):
        if key in self._data:
            self._data[key] = value
            self._data.move_to_end(key)
            return None
        self._data[key] = value
        if len(self._data) > self.capacity:
            evicted, _ = self._data.popitem(last=False)
            return evicted
        return None

    def pop(self, key):
        return self._data.pop(key)

    def size(self):
        return len(self._data)

    def keys(self):
        return list(self._data)
