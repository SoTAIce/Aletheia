# 任务：实现 merge_intervals

用 Python 实现函数 `merge_intervals(intervals) -> list[tuple[int, int]]`，合并重叠的闭区间。

## 规则
1. `intervals` 是列表或元组，其中每个元素是长度为 2 的列表或元组 `(start, end)`，表示闭区间 `[start, end]`。
2. 区间端点必须是 `int`（`bool` 不算整数），且 `start <= end`；允许负数，允许 `start == end` 的单点区间。
3. 两个区间有公共点就合并，端点相接也算（`(1, 4)` 和 `(4, 5)` 合并为 `(1, 5)`）；`(1, 2)` 和 `(3, 4)` 没有公共点，不合并。
4. 输入可以是任意顺序；返回值是按起点升序排列的新列表，每个元素都是元组 `(start, end)`。空输入返回空列表。
5. 不得修改传入的列表或其中的元素。
6. 以下情况抛出异常：
   - `intervals` 不是列表或元组：`TypeError`；
   - 某个元素不是列表或元组，或长度不为 2：`ValueError`；
   - 端点不是 `int`，或是 `bool`：`TypeError`；
   - `start > end`：`ValueError`。
7. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`，不得进行任何文件、网络或进程操作。

## 示例
- `merge_intervals([(1, 3), (2, 6), (8, 10), (15, 18)])` 返回 `[(1, 6), (8, 10), (15, 18)]`
- `merge_intervals([[8, 10], [1, 3], [2, 6]])` 返回 `[(1, 6), (8, 10)]`

## 交付
最终答案只包含完整的 Python 源码（一个模块，定义 `merge_intervals`），不要附加解释。
