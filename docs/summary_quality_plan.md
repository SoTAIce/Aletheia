# 后台摘要：输出上限和摘要语言（方案）

日期：2026-11-25。状态：**已实施，已用本机 Ollama 复测**（D-104）。第 8 节六条按推荐定（用户 2026-11-25）。

来源：2026-11-25 用本机 Ollama（`qwen3:4b-instruct`）做的实测（用户授权，只用编造的对话，脚本在临时目录，不进仓库）。整条链路是通的，但发现两个问题，第三个是读代码时顺带确认的。

下面的现状按当前代码（`d7d7d9c`）核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有你自己没提交的改动。

## 1. 实测结果

| 测什么 | 输入 | 结果 |
|---|---|---|
| 直接摘要 4 轮中文对话 | 约 3300 令牌 | 输出写满 512 被截断（`finish_reason=length`），判为失败；同样输入把上限放到 1500，实际用 699，六个小标题都对，但**正文是英文** |
| 网页整条链路（26 轮，每轮一个任务） | 约 1470 令牌 | 1.1 秒，输出 150～200，**中文**，小标题通过，提交进归档；网页历史仍是原文 |

速度没问题：模型加载后一次 1～4 秒，第一次含加载约 20 秒，都在调度器的 30 秒以内。

## 2. 问题

1. **512 的输出上限太小。** 网页上一个任务通常只有一轮，一条摘要只覆盖一个任务，现在够用。但下面两种情况一个批次会有好几轮，摘要就会被截断：
   - 一个任务有好几轮（追问后接着做，同一个 `task_id`）；
   - 没有任务编号的旧归档，最多 8 轮、6000 令牌合成一批（`ConversationMemory(summary_batch_size=8, summary_token_budget=6000)`）。
2. **摘要语言不稳定。** 提示词只说“正文用对话的主要语言”（`llm_compressor.py:24`），小模型在轮数多时会改写英文。以后召回时用户用中文提问，英文摘要按词法打分更难被找回（设计审查 3.5 节：摘要相关性是词法匹配）。
3. **最老的一批失败后，这个会话后面的摘要全停。**（读代码确认，没有实测）调度器对同一批次最多试 2 次，之后跳过（`summary_scheduler.py:88-89`）；但下一次选批次还是从最老的未摘要轮次开始（`conversation_archive.py:680` 的 `pending_summary_turns`），选出来还是同一批，于是这个进程里这个会话再也不会有新摘要。原文仍然可读，只是历史越来越长。问题 1 截断时就会撞上这一条。

## 3. 设计

### 3.1 输出上限（第 8 节第 1 问）

- `LLMCompressor` 的默认 `max_output_tokens` 从 512 改成 **1536**。这是上限不是目标，实测一个任务的摘要只用 150～200。
- 不按输入长度动态算：多一个规则多一处要测，固定上限已经够。
- 摘要进上下文的预算不变（`conversation_context.py:28`：最多 3 条正文，合计 2048 令牌或输入窗口的四分之一）；摘要变长时放进去的条数会少，这一点由现有预算逻辑自己处理。

### 3.2 摘要语言（第 8 节第 2 问）

- 宿主判断对话的主要语言：数这一批轮次所有消息里的汉字和拉丁字母，**汉字占两者之和的 30% 以上**就算中文。
- 是中文时，在用户消息（不是系统提示）末尾多一行：`Write the body text in Simplified Chinese; keep the headings exactly as given in English.`；不是中文时请求逐字不变。
- 系统提示词 `SYSTEM_PROMPT` 不改，小标题仍是六个英文标题，检查规则不变。
- 只判中文和“其他”两种，不做通用语言识别（不加依赖）。

### 3.3 失败的批次不再卡住后面（第 8 节第 3 问）

- 只处理“输出被截断”这一种确定会重复的失败：`LLMCompressor` 遇到 `finish_reason == 'length'` 时抛一个新的 `SummaryTooLongError`（仍是 `RuntimeError` 的子类，现有捕获不受影响）。
- `ConversationMemory` 收到它、且这一批多于 1 轮时，下一次选批次时把这个会话的批次轮数上限减半（8 → 4 → 2 → 1），批次变了，调度器的重试计数自然重新开始。上限只在内存里，进程重启后回到 8。
- 只剩 1 轮还被截断：照旧试满后放弃，并且把这一轮记为“跳过”，后面的轮次继续摘要。跳过的轮次原文照旧可读，只是没有摘要；这件事只记一条日志警告（只有类型名，D-033）。
- 其他失败（连不上、超时、小标题不对）照旧：同一批试 2 次后放弃，不改。

### 3.4 要改的文件

- 修改：
  - `app/services/memory_compression/llm_compressor.py`：默认上限 1536、`SummaryTooLongError`、语言判断和那一行；
  - `app/models/conversation_memory.py`：批次轮数上限减半、单轮跳过；
  - `app/services/conversation_archive.py`：`pending_summary_turns` 多一个参数，跳过指定的轮次编号（只读，不改归档格式）；
  - `app/agent/run_limits.py`：`--show-limits` 的“后台摘要”一行不变（不显示输出上限），或者多一行“单次摘要输出上限 1536 令牌”——见第 8 节第 4 问。
- 新增：`tests/test_summary_quality.py`。
- 不改：调度器、归档文件格式、摘要进上下文的预算、`SYSTEM_PROMPT`、六个小标题、模型档案、黄金对照文件、依赖。

## 4. 测试（离线）

- 默认上限：请求里 `max_tokens == 1536`；传了 `max_output_tokens` 时照旧用传入的值。
- 语言：全中文、中英混合（汉字过 30%）、全英文三种批次，前两种用户消息末尾有那一行，第三种和改动前逐字相同。
- 截断：假客户端返回 `finish_reason='length'` → 抛 `SummaryTooLongError`；8 轮的批次下次变成 4 轮，再变 2、1；1 轮仍截断时这一轮被跳过，下一轮能被摘要，日志里只有类型名。
- 其他失败（连接错误、小标题缺失）的行为和现在相同。
- 现有 `tests/test_summary_scheduler.py`、`tests/agent/test_summary_lifecycle_e2e.py`、`tests/test_history_compression_setup.py`、`tests/test_summarizer_role.py` 的断言不改。

## 5. 不做的

- 按输入长度动态算输出上限；
- 通用语言识别、按语言切换小标题；
- 把跳过的轮次写进归档（重启后会重新尝试一次，可以接受）；
- 改调度器的重试次数和时限；
- 远端摘要模型的实测（DashScope 账号目前只用免费额度，被拒）。

## 6. 风险

- **摘要变长占上下文**：上限放到 1536 后，多轮批次的摘要可能接近 1000 令牌，一次最多放进去的摘要条数会少。单任务的摘要实测只有 150～200，影响小。
- **30% 的门槛是拍的**：中文对话里夹大量代码或英文日志时可能判成“其他”，那样就和现在一样，不会更坏。
- **跳过的轮次没有摘要**：只在单轮也写不完时发生，原文仍可读、可检索。
- **改完要再实测一次**：离线测试只能证明请求和流程对，模型是否真的改写中文、多轮批次是否不再截断，要你同意后用本机 Ollama 再跑一次同样的脚本（不花钱）。

## 7. 实施

照旧由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写实施说明并核对，Gemini 结束前清理无关文件并前台跑 `init.ps1`；离线测试通过后，经你同意再用本机 Ollama 复测。

## 8. 要用户定的

1. **输出上限**：推荐**固定改成 1536**。
2. **摘要语言**：推荐**宿主判断，汉字过 30% 就在用户消息里加一行要求简体中文**，系统提示和小标题不变。
3. **失败批次卡住后面**：推荐**只对“输出被截断”减半批次，单轮仍截断就跳过这一轮**；其他失败不改。
4. **`--show-limits`**：推荐**多一行“单次摘要输出上限 1536 令牌”**。
5. **实施和复测**：推荐 Gemini 实施、Claude 核对，离线通过后用本机 Ollama 复测一次。
6. **记录**：推荐新开 **D-104**。

## 9. 实施记录

日期：2026-11-25。按第 8 节推荐项和实施说明完成离线代码与测试改动（未运行本机 Ollama 或远端模型，未改动 `docs/DECISIONS.md`、`app/services/conversation_archive.py`、`app/services/summary_scheduler.py`）：

- `app/services/memory_compression/llm_compressor.py`：新增 `DEFAULT_MAX_OUTPUT_TOKENS = 1536` 并作为 `LLMCompressor` 默认 `max_output_tokens`；新增 `SummaryTooLongError(RuntimeError)`，在检查 choices 数量后、其他不完整检查前对 `finish_reason == "length"` 抛出；新增纯函数 `_mostly_chinese(turns)`（汉字 `\u4e00-\u9fff` 与 ASCII 字母占比 ≥ 30%，不计 `previous_summary`），命中时在 `_build_prompt` 返回值末尾追加简体中文正文要求，非中文批次与改动前逐字一致。
- `app/models/conversation_memory.py`：新增内存态 `_batch_limit` 与 `_skipped_turn_ids`；`_refresh_pending_locked` 使用 `_batch_limit` 并将 `_skipped_turn_ids` 并入传给 `pending_summary_turns` 的 `covered`；`_compress_archive` 遇到 `SummaryTooLongError` 时多轮批次减半、单轮批次记录跳过并仅以异常类型名写警告日志（沿用模块现有 `loguru` 格式化 `{}`），提交摘要后复位 `_batch_limit`，`clear()` 和 `restore()` 清空跳过集合并复位 `_batch_limit`。
- `app/agent/run_limits.py`：`--show-limits` 在“后台摘要”原行后新增 `('后台摘要', '单次摘要输出上限', f'{DEFAULT_MAX_OUTPUT_TOKENS} 令牌')`，原行逐字不变。
- `tests/test_summary_quality.py`：新增 10 项离线测试，覆盖默认/显式输出上限、四种语言批次与英文逐字比对、输入预算计入中文提示行、`SummaryTooLongError` 判断顺序、批次减半与提交后复位、单轮跳过与日志脱敏、`SummaryScheduler` 配合、其他失败不变以及 `--show-limits`。
- `tests/test_summarizer_role.py`：`test_open_conversation_compressor_with_profile_uses_selected_fields` 中断言默认值从 `512` 同步为 `1536`。
- `tests/agent/test_summary_lifecycle_e2e.py`：`test_full_agent_lifecycle_with_background_summary_faults` 的 `mode == 'truncated'` 分支原断言停在第 2 次调用（旧行为：同一批截断试 2 次后卡停）；现改为断言减半并跳过单轮后最终停止且无摘要写入，其他故障模式断言不变。
- Claude 核对（2026-11-25）：
  - 逐个看过三个代码文件和两处测试断言的改动，没有改动；
  - 和方案有一处出入，接受：单轮批次第一次被截断就跳过，不再按方案 3.3 写的“试满后再跳过”；温度 0.1 下重试同一轮多半还是截断；
  - 方案 3.4 列的 `conversation_archive.py` 没有改：跳过的轮次直接并进 `covered` 集合；
  - `tests/agent/test_summary_lifecycle_e2e.py` 的 `truncated` 分支改了断言：旧断言写的正是“同一批试 2 次后卡住”，这次就是要改掉这个行为；
  - 自己重跑 `init.ps1` 通过：2461 passed、28 skipped（基线 2451，新增 10 个），`tests/fixtures/` 没有变化，改过的文件都是 LF；你没提交的文件没有被碰过。

## 10. 本机复测（2026-11-25，用户授权）

本机 Ollama 0.40.16，`qwen3:4b-instruct`，只用编造的对话；脚本和临时目录都在仓库外，跑完已删除。

| 测什么 | 改动前 | 改动后 |
|---|---|---|
| 直接摘要 4 轮中文对话（约 3450 令牌） | 写满 512 被截断，判为失败；上限放宽后正文是英文 | 511 令牌自己结束，**中文**，小标题通过 |
| 网页链路，每轮一个任务（约 1490 令牌） | 150～200 令牌，中文 | 中文，小标题通过；三次里两次输出约 1380 令牌、一次 209 令牌 |
| 网页链路，没有任务编号的旧对话（一批 6 轮） | 未测 | 两次都被 Ollama 以 `BadRequestError` 拒绝，没有摘要 |

复测发现两个新问题，这次都没有改：

1. **上限放宽后，摘要有时几乎和原文一样长。** 一轮约 1490 令牌的对话，摘要有时写到约 1380 令牌，把 24 条样本记录逐条抄进去，等于没有压缩。以前 512 的上限会把这种摘要截断判为失败；现在会被接受，进上下文时更占地方。可以考虑的做法：提示词要求“不要逐条抄数据记录”，或者摘要超过输入的一半就判为失败并重试。
2. **Ollama 默认上下文只有 4096 令牌。** 6 轮的旧对话批次请求约 7700 令牌（输入加上 1536 的输出上限），超过 Ollama 默认的 4096，被拒绝。这和本次改动无关，改动前一样（上限是 512 时也超过）。批次失败不属于“输出被截断”，所以不会减半，这个会话照旧卡在这一批。可以考虑的做法：启动 Ollama 时设 `OLLAMA_CONTEXT_LENGTH=8192`，或者按摘要模型的窗口缩小批次预算，或者把 `BadRequestError` 也当作“批次太大”处理。

这两个问题已由 D-105 处理，见 [摘要窗口方案](summary_fit_plan.md)。

另外，复测时 `RagAgentService` 默认会往仓库的 `data/personas/` 写会话的人设状态；脚本后来改为传 `personas_dir` 到临时目录，之前两次留下的 `data/personas/` 已确认是测试会话并删除。原有的 `logger.exception('Stage summary failed; ...')` 会把服务商错误原文写进日志（例如上面的 400 说明），和 D-033 的约定不一致，是以前就有的，这次没改。
