# allocate.py 规格

`allocate.py` 按权重分摊以“分”为单位的整数金额，保证分摊结果之和恰好等于总额。

1. `allocate(total_cents: int, weights) -> list[int]`：
   - `total_cents` 必须是 `int`（`bool` 不算），否则抛 `TypeError`；小于 0 抛 `ValueError`；
   - `weights` 必须是列表或元组，否则抛 `TypeError`；为空抛 `ValueError`；
   - 每个权重必须是 `int`（`bool` 不算），否则抛 `TypeError`；任一权重为负，或全部权重之和为 0，抛 `ValueError`；
   - 记 `S` 为权重之和。第 `i` 份先取 `total_cents * weights[i] // S`（向下取整，必须用整数运算，不能用浮点数）；
   - 剩余的分数 `total_cents - 各份之和` 逐个分给余数 `total_cents * weights[i] % S` **最大**的那几份，每份最多多分 1 分；余数相同时下标**较小**的优先；
   - 返回与 `weights` 等长的新列表，权重为 0 的位置结果为 0。
2. `split_even(total_cents: int, parts: int) -> list[int]`：等价于 `allocate(total_cents, [1] * parts)`；`parts` 不是 `int` 或是 `bool` 抛 `TypeError`，小于 1 抛 `ValueError`。
3. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`，不得进行任何文件、网络或进程操作。

示例：`allocate(100, [1, 1, 1])` 返回 `[34, 33, 33]`；`allocate(10, [1, 1, 5])` 返回 `[2, 1, 7]`。
