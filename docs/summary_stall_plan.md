# 后台摘要：失败的批次不再让后面的摘要停下（方案）

日期：2026-11-26。状态：**已实施，已做端到端复测**（D-106）。第 8 节六条按推荐定（用户 2026-11-26）。

来源：2026-11-26 真实端到端测试（用户授权；网页后端开 `--compress-history`，本机 Ollama 写摘要，真实主模型答题）。前 4 批摘要都成功了，第 5 批连续两次没按六个小标题输出（`RuntimeError: compression model omitted or duplicated required summary sections`），调度器放弃这一批以后，这个会话剩下的 4 轮旧对话再也没有摘要。

下面的现状按当前代码（`2298d44`）核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有你自己没提交的改动。

## 1. 为什么会停下

- 调度器按“批次标识”记失败次数，同一个标识试满 2 次就不再试（`summary_scheduler.py` 的 `_compress`）。
- 批次标识是 `(revision, 这一批的轮次编号)`（`conversation_memory.py:428`）。`revision` 只在清空会话时变，下一次选批次又总是从最老的未摘要轮次开始，所以选出来的还是同一批、同一个标识。
- D-104、D-105 只给三种失败开了出路：输出截断、输入超限（减半或跳过）、摘要过长（第二次收下）。**其他失败都会卡住**：
  - **内容不合格**：小标题缺失或重复、空摘要、被拒绝或回答不完整、返回的候选不是正好一个。这次撞上的就是小标题不对；
  - **连接类失败**：连不上、超时、服务商 5xx 或限流。Ollama 中途退出后又重新启动时，这个会话在这次运行里也不会再有摘要，要重启网页后端才行。

## 2. 设计

### 2.1 内容不合格：第二次失败就减半或跳过（第 8 节第 1 问）

- `LLMCompressor` 新增 `SummaryContentError(RuntimeError)`，上面四种“内容不合格”都改抛它，说明文字不变。`ConversationMemory` 里“压缩器返回空摘要”那一处也改抛它。
- `ConversationMemory` 按 D-105 处理“过长”的办法，记下这一批第一次内容不合格（只在内存里），然后照常失败，调度器约 1 秒后重试同一批。
- **同一批第二次还是内容不合格**：多于 1 轮就把批次上限减半，只有 1 轮就跳过这一轮，记一条警告，只写类型名。批次变了，标识跟着变，调度器的计数会重新开始。
- 只要提交成功一条摘要，或者清空、恢复会话，这个记录就清掉。

### 2.2 连接类失败：冷却后重新尝试（第 8 节第 2 问）

- 调度器对同一批试满 2 次以后，不再永远跳过，而是**冷却 10 分钟**（新参数 `cooldown_seconds=600`）后清零计数，等下一次扫描时重新接纳。
- 冷却期间不重试，不会在 Ollama 没开时每分钟去撞一次。
- 这一条对所有失败都生效，内容不合格也一样。但内容不合格在 2.1 里第二次就换了批次，一般走不到冷却这一步。

### 2.3 日志不写错误原文（第 8 节第 3 问）

- 现在摘要失败时，`conversation_memory.py` 用 `logger.exception` 记日志，会把完整的调用栈和服务商的错误原文写进去。端到端测试里看到过 Ollama 的 400 说明，那条说明里带着请求的令牌数。这不符合 D-033“只记类型名”的约定。
- 改成 `logger.warning('Stage summary failed ({}); archived originals were retained', type(exc).__name__)`，不再记调用栈。调度器那一行本来就只写类型名，不改。

### 2.4 要改的文件

- 修改：
  - `app/services/memory_compression/llm_compressor.py`：`SummaryContentError` 和四处改抛；
  - `app/models/conversation_memory.py`：第二次内容不合格时减半或跳过；空摘要改抛新错误；日志改成只写类型名；
  - `app/services/summary_scheduler.py`：`cooldown_seconds`，试满后冷却再清零；
  - `app/agent/run_limits.py`：`--show-limits` 多一行“摘要失败后的冷却”，写 10 分钟。
- 新增：`tests/test_summary_stall.py`。
- 不改：归档格式、选批次的规则、D-104 和 D-105 已有的三种处理、提示词、小标题、黄金对照文件、依赖。

## 3. 测试（离线）

- 小标题缺失：同一批第一次失败、第二次单轮被跳过，下一轮能被摘要；多轮批次第二次减半。
- 只有第一次内容不合格：第二次正常的摘要被提交，没有轮次被跳过。
- 空摘要、被拒绝、候选数不对各一个用例，都抛 `SummaryContentError`。
- 调度器用假时钟：试满 2 次后冷却期内不再调用；冷却结束后重新调用，成功就提交。
- 日志：失败时只有类型名，没有错误原文、调用栈或摘要内容（用 loguru 接收器检查）。
- 现有的 `tests/test_summary_scheduler.py`、`tests/agent/test_summary_lifecycle_e2e.py`、`tests/test_summary_quality.py`、`tests/test_summary_fit.py`：断言尽量不改；因为“放弃后会在冷却后重试”或“不再记调用栈”而必须改的，报告里逐个列出原因。

## 4. 不做的

- 把跳过的轮次写进归档：重启后会重新试一次，可以接受；
- 区分 5xx 和 400：都按 2.2 冷却；
- 换温度或换提示词重试；
- 跳过的轮次在网页上提示：原文照样能看、能被找回。

## 5. 风险

- **跳过的轮次没有摘要**：要连续两次都写不出合格摘要才会跳过。这次测试里 5 批中有 1 批这样，比例不低，说明 4B 的小模型偶尔不守格式。跳过总比整个会话停下好。
- **冷却 10 分钟是拍的**：太短会在 Ollama 关着时反复撞，太长会让恢复变慢。
- **改了日志写法**：以后排查摘要失败时，只能看到类型名，看不到原文。要排查就开轨迹，或者手动复现。

## 6. 复测

改完离线测试通过后，经你同意，用同样的方式再跑一次端到端测试：本机 Ollama 加网页后端，预先放 26 轮对话，发 2 条真实消息，大约 2.7 万主模型令牌，看全部积压的旧轮次能不能都处理完（有摘要或被跳过）。

## 7. 实施

照旧由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写实施说明并核对，Gemini 结束前清理无关文件并前台跑 `init.ps1`。

## 8. 要用户定的

1. **内容不合格**：推荐**同一批第二次还不合格就减半或跳过**。
2. **连接类失败**：推荐**试满 2 次后冷却 10 分钟再试**，不再在这次运行里永远放弃。
3. **日志**：推荐**摘要失败的日志改成只写类型名**，符合 D-033。
4. **复测**：推荐离线通过后**再跑一次端到端**，大约 2.7 万主模型令牌，需要你同意。
5. **实施**：推荐照旧由 Gemini 实施、Claude 核对，结束前清理无关文件。
6. **记录**：推荐新开 **D-106**。

## 9. 实施记录

日期：2026-11-26。按第 8 节推荐项与实施说明完成离线代码与测试改动（未连接本机 Ollama 或远端模型，未改动 `docs/DECISIONS.md`、`docs/PROJECT_HANDOVER.md`、`README.md`、`tests/fixtures/`）：

- `app/services/memory_compression/llm_compressor.py`：新增 `SummaryContentError(RuntimeError)`，将候选数不为 1、不完整/被拒绝、空摘要、小标题缺失或重复四处 `RuntimeError` 改抛 `SummaryContentError`，说明文字保持不变。
- `app/models/conversation_memory.py`：
  - 新增内存状态 `_content_failed_batch`（在 `restore()`、`clear()` 与成功提交时复位）；
  - `_compress_archive` 中空摘要改抛 `SummaryContentError('compressor returned an empty summary')`；
  - 外层 `except` 新增 `SummaryContentError` 分支：首次记录批次、不减半不跳过；同一批次第二次失败时清零记录，多于 1 轮减半 `_batch_limit`、单轮加入 `_skipped_turn_ids` 并以类型名记警告；在 `SummaryBatchTooLargeError` 和 `SummaryContentError` 改动 `_batch_limit` 或 `_skipped_turn_ids` 后立即刷新待摘要缓冲，使调度器立刻看到更新后的批次标识；
  - `_compress_archive` 与 `_compress_buffer_locked` 两处 `logger.exception(...)` 改为 `logger.warning(...)` 并只记录异常类型名（D-033），不再写调用栈或错误原文。
- `app/services/summary_scheduler.py`：
  - `__init__` 新增 `cooldown_seconds=600.0`（校验有限、正数）；
  - `_compress` 开头检查同一批次标识且试满 `max_attempts` 时，冷却期内返回 `None`，到期后清零失败计数并继续执行；
  - 失败后若待摘要批次已在 `compress_pending` 内变更（减半或跳过），清零旧批次失败计数并按 `retry_delay_seconds` 继续处理新批次；否则同一批次试满 `max_attempts` 时把 `not_before` 设为 `time.monotonic() + self._cooldown` 并以 `%g`/`%s` 记录仅含数字和异常类型名的冷却警告。周期 `_scan` 会在冷却期间或到期后重新 `notify`，`notify` 按 `not_before` 排期，冷却到期后自动重试。
- `app/agent/run_limits.py`：`--show-limits` 的“后台摘要”段新增 `('后台摘要', '摘要失败后的冷却', '600 秒')`（从 `SummaryScheduler.__init__` 默认参数读取）。
- `tests/test_summary_stall.py`：新增 20 项离线测试（10 个测试函数，含参数化用例），覆盖四类 `SummaryContentError`、自定义压缩器空摘要、单轮二次失败跳过与下一轮摘要、多轮二次失败减半与复位、仅首次失败后正常提交、调度器配合内容错误恢复、冷却参数校验与真实调度器冷却后重试、两处日志脱敏及 `--show-limits`；现有测试断言未改动。
- Claude 核对（2026-11-26）：
  - 逐个看过压缩器、对话记忆、调度器和限额表的改动，没有改动；
  - Gemini 多做了一处配合，接受：对话记忆减半或跳过后立刻刷新待摘要批次，调度器发现批次标识变了就清零计数、约 1 秒后接着处理新批次。不这样做，减半或跳过后会被误算成旧批次试满，进入 10 分钟冷却。代价是调用前的批次标识过期时，连接类失败可能多试一次，可以接受；
  - 现有测试断言一条没改；自己重跑 `init.ps1`：2503 passed、28 skipped（基线 2483，新增 20 个），`tests/fixtures/` 没有变化，改过的文件都是 LF；你没提交的文件没有被碰过，`data/` 没有新东西；
  - 没有连接 Ollama 或服务商。

## 10. 端到端复测（2026-11-26，用户授权）

做法和发现问题的那次相同：本机 Ollama（默认 4096 窗口）加网页后端 `--compress-history`，数据、人设、令牌都在临时目录，工作目录是 D 盘仓库外的空目录；预放 26 轮编造对话，发 2 条真实消息（中转主模型，11999 + 15209 令牌）。测完服务全部关闭，临时目录删除，`data/` 不变。

| 看什么 | 结果 |
|---|---|
| 后台摘要 | 会话加载约 5 分钟后开始，积压的 7 轮**全部摘要完**（7 条，每条 1 轮），没有批次失败，没有跳过 |
| 网页历史 | 28 轮都是原文 |
| 主模型回答 | **两次都答非所问**：第一次回了 `{"timezone":"UTC"}`，第二次回了一段说找不到 `bash` 工具的 JSON；两次运行都带“服务商回答所用的模型和请求的不同”的提示（D-079） |

结论：

- 这次所有批次第一次就成功，新增的“第二次不合格就跳过”和“冷却后再试”在真实运行里**没有被触发**，只有离线测试覆盖。小模型不守格式是偶发的，上一次 5 批里有 1 批。
- 主模型的问题和摘要无关：上一次同样的问题答对了，这次中转服务给出的模型和请求的不同，回答不可靠。任务仍被判为“完成”。这说明中转服务可能在悄悄换模型，D-079 只提示、不拦截。
