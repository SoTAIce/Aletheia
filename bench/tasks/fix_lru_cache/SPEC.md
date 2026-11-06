# lru_cache.py 规格

`lru_cache.py` 提供一个容量固定的最近最少使用缓存 `LRUCache`。“使用”指 `get` 命中或 `put` 写入。

1. `LRUCache(capacity: int)`：`capacity` 不是 `int` 或是 `bool` 时抛 `TypeError`；小于 1 时抛 `ValueError`。
2. `get(key, default=None)`：键存在时返回其值，并把该键标记为最近使用；键不存在时返回 `default`，不改变任何顺序。值本身可以是 `None`。
3. `peek(key, default=None)`：与 `get` 相同，但不改变使用顺序。
4. `put(key, value) -> 被淘汰的键或 None`：
   - 键已存在：更新其值并标记为最近使用，不淘汰任何键，返回 `None`；
   - 键不存在：插入并标记为最近使用；若插入后条目数超过 `capacity`，淘汰最近最少使用的键并返回该键，否则返回 `None`。
5. `pop(key)`：删除并返回该键的值；键不存在时抛 `KeyError`。
6. `size() -> int`：当前条目数。
7. `keys() -> list`：按从最近最少使用到最近使用的顺序返回所有键。
8. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`，不得进行任何文件、网络或进程操作；除 `__init__` 外不要定义其他双下划线方法。
