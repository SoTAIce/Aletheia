# word_wrap.py 规格

`word_wrap.py` 提供函数 `wrap(text: str, width: int) -> list[str]`，把文本按宽度自动换行。

1. `text` 不是 `str` 时抛 `TypeError`；`width` 不是 `int` 或是 `bool` 时抛 `TypeError`；`width` 小于 1 时抛 `ValueError`。
2. 单词是被任意空白字符（空格、制表符、换行等，即 `str.split()` 的切分方式）隔开的非空片段；连续空白、首尾空白都不产生空单词。
3. 贪心填充：按顺序放置单词，同一行的单词之间用一个空格连接；只要放入下一个单词后该行长度**小于或等于** `width`，就放在当前行，否则另起一行。
4. 长度超过 `width` 的单词要被切开：如果当前行已有内容，先结束当前行；然后从该单词开头每 `width` 个字符切出一段，每段单独成一行；最后剩下的不足或等于 `width` 的部分作为新的当前行，后续单词放得下时可以接在它后面。
5. 返回的每一行都不含首尾空白，长度都不超过 `width`。没有单词时返回空列表。
6. 只能导入 Python 标准库中的 `re`、`math`、`typing`、`dataclasses`、`collections`、`functools`、`itertools`、`string`，不得进行任何文件、网络或进程操作。

示例：`wrap('abcdefg hi', 5)` 返回 `['abcde', 'fg hi']`；`wrap('the quick brown fox', 10)` 返回 `['the quick', 'brown fox']`。
