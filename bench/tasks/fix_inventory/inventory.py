class Inventory:
    def __init__(self):
        self._items = {}

    def add(self, sku, qty):
        if qty < 0:
            raise ValueError('qty must be positive')
        self._items[sku] = qty

    def remove(self, sku, qty):
        if qty <= 0:
            raise ValueError('qty must be positive')
        self._items[sku] = self._items.get(sku, 0) - qty
        if self._items[sku] < 0:
            raise ValueError('insufficient stock')

    def count(self, sku):
        return self._items[sku]

    def page(self, page, size):
        skus = sorted(self._items)
        start = page * size
        return skus[start:start + size]

    def total_value(self, prices):
        return round(sum(prices.get(sku, 0) * qty for sku, qty in self._items.items()), 2)
