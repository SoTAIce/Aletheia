# inventory.py 规格

`inventory.py` 提供一个内存库存类 `Inventory`。

1. `add(sku: str, qty: int)`：qty 必须是正整数，否则抛 `ValueError`；同一 sku 累加数量。
2. `remove(sku: str, qty: int)`：qty 必须是正整数；sku 不存在或库存不足时抛 `ValueError`，且库存保持不变；数量减到 0 时删除该 sku。
3. `count(sku: str) -> int`：返回数量，不存在返回 0。
4. `page(page: int, size: int) -> list[str]`：按 sku 字典序排序后分页，page 从 1 开始；page 或 size 小于 1 抛 `ValueError`；超出范围返回空列表。
5. `total_value(prices: dict[str, float]) -> float`：按 `prices` 计算总价值，缺少价格的 sku 抛 `KeyError`；结果四舍五入到 2 位小数。
