"""Shopping cart helpers. Items are dicts like {"sku": "A1", "price": 2.5, "qty": 3}."""

_REGISTRY = {}
TAX_RATE = 0.2


def subtotal(items):
    return sum(item["price"] * item["qty"] for item in items)


def with_tax(items):
    return subtotal(items) * (1 + TAX_RATE)


def add_item(items, item):
    items.append(item)
    return items


def with_item(items, item):
    return items + [item]


def sorted_by_price(items):
    result = list(items)
    result.sort(key=lambda item: item["price"])
    return result


def sort_by_price(items):
    items.sort(key=lambda item: item["price"])


def apply_coupon(items, percent):
    for item in items:
        item["price"] = item["price"] * (100 - percent) / 100
    return items


def discounted(items, percent):
    return [dict(item, price=item["price"] * (100 - percent) / 100) for item in items]


def register(name, items):
    _REGISTRY[name] = list(items)


def lookup(name):
    return _REGISTRY.get(name, [])


def remember(item, history=[]):
    history.append(item["sku"])
    return list(history)


def skus(items):
    seen = {}
    for item in items:
        seen.setdefault(item["sku"], 0)
        seen[item["sku"]] += item["qty"]
    return sorted(seen)


def print_receipt(items):
    for item in items:
        print(f'{item["sku"]:<6}{item["price"] * item["qty"]:>8.2f}')


def checkout(items):
    print_receipt(items)
    return with_tax(items)
