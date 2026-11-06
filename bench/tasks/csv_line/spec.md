# 任务：实现 parse_csv_line

用 Python 实现函数 `parse_csv_line(line: str, sep: str = ',') -> list[str]`，把 CSV 的一行解析为字段列表。不能使用 `csv` 模块。

## 规则
1. 字段之间用分隔符 `sep` 隔开。`n` 个分隔符产生 `n + 1` 个字段：空字符串返回 `['']`，`'a,'` 返回 `['a', '']`。
2. 字段内容原样保留，不去掉首尾空白（`' a , b '` 返回 `[' a ', ' b ']`）。
3. 以双引号 `"` 开头的字段是引号字段，一直到与之配对的结束引号为止：
   - 引号内可以出现分隔符和换行符（`\n`、`\r`），它们都属于字段内容；
   - 引号内连续两个双引号 `""` 表示一个双引号字符；
   - 结束引号后必须紧跟分隔符或行尾，否则非法（`'"a"b'`、`'"a" ,b'` 都非法）；
   - 返回的字段不包含外层引号（`'""'` 返回 `['']`）。
4. 非引号字段中不允许出现双引号，也不允许出现换行符 `\n` 或 `\r`。
5. 以下情况抛出异常：
   - `line` 或 `sep` 不是 `str`：`TypeError`；
   - `sep` 的长度不是 1，或 `sep` 是 `"`、`\n`、`\r`：`ValueError`；
   - 引号没有闭合、结束引号后有多余字符、非引号字段中出现双引号或换行符：`ValueError`。
6. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`，不得进行任何文件、网络或进程操作。

## 示例
- `parse_csv_line('a,"b,c",d')` 返回 `['a', 'b,c', 'd']`
- `parse_csv_line('"he said ""hi""",x')` 返回 `['he said "hi"', 'x']`
- `parse_csv_line('a;b', sep=';')` 返回 `['a', 'b']`

## 交付
最终答案只包含完整的 Python 源码（一个模块，定义 `parse_csv_line`），不要附加解释。
