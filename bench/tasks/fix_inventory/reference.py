"""Hidden reference fix for inventory.py; never copied into the agent's root."""


class Inventory:
    def __init__(self):
        self._items = {}

    @staticmethod
    def _check_qty(qty):
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise ValueError('qty must be a positive integer')

    def add(self, sku, qty):
        self._check_qty(qty)
        self._items[sku] = self._items.get(sku, 0) + qty

    def remove(self, sku, qty):
        self._check_qty(qty)
        have = self._items.get(sku)
        if have is None or have < qty:
            raise ValueError('unknown sku or insufficient stock')
        if have == qty:
            del self._items[sku]
        else:
            self._items[sku] = have - qty

    def count(self, sku):
        return self._items.get(sku, 0)

    def page(self, page, size):
        if page < 1 or size < 1:
            raise ValueError('page and size start at 1')
        skus = sorted(self._items)
        start = (page - 1) * size
        return skus[start:start + size]

    def total_value(self, prices):
        return round(sum(prices[sku] * qty for sku, qty in self._items.items()), 2)
