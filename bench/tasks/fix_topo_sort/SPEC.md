# topo_sort.py 规格

`topo_sort.py` 提供依赖排序函数 `topo_sort(deps: dict) -> list[str]` 和异常类 `CycleError`。

1. `deps` 把每个节点映射到它的前置节点集合：`deps['b'] = ['a']` 表示 `a` 必须排在 `b` 之前。
2. 节点都是 `str`。前置节点的容器可以是列表、元组或集合；同一个前置节点重复出现只算一次。
3. 只作为前置节点出现、自身不是 `deps` 的键的节点，也要出现在结果中。
4. 结果是确定的：每一步在所有前置节点都已排出的节点中，选择字典序最小的一个排出（Kahn 算法配合最小优先）。例如 `{'z': [], 'y': [], 'a': ['y']}` 的结果是 `['y', 'a', 'z']`。
5. 存在环（包括节点依赖自身）时抛出 `CycleError`，而不是返回部分结果。`CycleError` 是 `ValueError` 的子类，它的 `nodes` 属性是无法排出的全部节点按字典序排成的列表（环上的节点以及直接或间接依赖它们的节点）。
6. 不得修改传入的 `deps` 及其中的容器。
7. 以下情况抛 `TypeError`：`deps` 不是 `dict`；某个键或前置节点不是 `str`；某个值不是列表、元组或集合（注意字符串不算）。
8. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`，不得进行任何文件、网络或进程操作；除 `__init__` 外不要使用其他双下划线方法或属性。
