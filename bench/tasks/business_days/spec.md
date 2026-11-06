# 任务：实现 count_business_days

用 Python 实现函数 `count_business_days(start: str, end: str, holidays=()) -> int`，统计两个日期之间（含首尾两天）的工作日天数。

## 规则
1. 日期字符串格式严格为 `YYYY-MM-DD`：4 位年、2 位月、2 位日，只能用 ASCII 数字和 `-`，不允许空白或其他分隔符。
2. 使用公历规则：年份范围 `0001` 到 `9999`；闰年是能被 4 整除但不能被 100 整除的年份，或能被 400 整除的年份（`2000-02-29` 合法，`1900-02-29` 不合法）。月份和日期必须真实存在。
3. 工作日是星期一到星期五。已知 `0001-01-01` 是星期一，`2024-01-01` 也是星期一。
4. `holidays` 是由日期字符串组成的列表、元组或集合；其中落在区间内且是工作日的日期不计入结果。重复的节假日只扣一次，落在周末或区间外的节假日不影响结果。
5. 返回 `int`。`start` 与 `end` 相同时只看这一天。
6. 以下情况抛出异常：
   - `start`、`end` 不是 `str`，`holidays` 不是列表、元组或集合，或 `holidays` 中有元素不是 `str`：`TypeError`；
   - 任何日期（包括 `holidays` 中的每一个）格式不合法或不存在：`ValueError`；
   - `start` 晚于 `end`：`ValueError`。
7. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`；**不能**使用 `datetime`、`calendar`、`time`，需要自己实现日期计算。不得进行任何文件、网络或进程操作。
8. 区间可能跨越上千年，实现不能逐日循环到超时（整个检查在几秒内完成）。

## 示例
- `count_business_days('2024-03-04', '2024-03-10')` 返回 `5`（周一到周日）
- `count_business_days('2023-12-29', '2024-01-02')` 返回 `3`
- `count_business_days('2024-12-23', '2024-12-27', ['2024-12-25'])` 返回 `4`

## 交付
最终答案只包含完整的 Python 源码（一个模块，定义 `count_business_days`），不要附加解释。
