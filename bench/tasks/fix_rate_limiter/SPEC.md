# rate_limiter.py 规格

`rate_limiter.py` 提供令牌桶限流器 `TokenBucket`。时间由调用方以秒为单位传入，类本身不读取系统时钟。

1. `TokenBucket(capacity: int, rate: float, now: float = 0.0)`：
   - `capacity` 必须是正整数（`bool` 不算），否则抛 `ValueError`；
   - `rate` 是每秒补充的令牌数，必须是大于 0 的数（`bool` 不算），否则抛 `ValueError`；
   - 创建时桶是满的（令牌数等于 `capacity`），上次补充时间记为 `now`。
2. 补充规则：每次调用 `allow` 或 `available` 时，先按 `(now - 上次补充时间) * rate` 补充令牌，补充后的令牌数**不超过** `capacity`，再把上次补充时间更新为 `now`。
3. `now` 早于上次补充时间（时间倒退）时抛 `ValueError`，且令牌数和上次补充时间都保持不变；`now` 等于上次补充时间是合法的。
4. `allow(now: float, cost: int = 1) -> bool`：
   - `cost` 必须是正整数（`bool` 不算）且不超过 `capacity`，否则抛 `ValueError`，状态保持不变；
   - 补充之后，若令牌数**大于或等于** `cost`，扣除 `cost` 个令牌并返回 `True`；
   - 否则返回 `False`，且**不扣除**任何令牌。
5. `available(now: float) -> float`：补充之后返回当前令牌数，不扣除令牌。
6. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`，不得进行任何文件、网络或进程操作。
