# 后台摘要：摘要过长、摘要模型的窗口（方案）

日期：2026-11-25。状态：**已实施，已用本机 Ollama 复测**（D-105）。第 8 节八条按推荐定（用户 2026-11-25）。

来源：`docs/summary_quality_plan.md` 第 10 节，D-104 本机复测时发现、当时没改的两个问题。下面的现状按当前代码（`9dc649c`）和本机 Ollama 0.40.16（`qwen3:4b-instruct`）核实过；补充测量只发了 `max_tokens=1` 的请求，不花钱。

本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有你自己没提交的改动。

## 1. 问题

1. **摘要有时几乎和原文一样长。** 一轮约 1490 令牌的对话，三次里两次写到约 1380 令牌，把 24 条样本记录逐条抄进去，等于没压缩。D-104 把上限放到 1536 以后，这种摘要会被收下，进上下文时更占地方。
2. **请求超过摘要模型的窗口。** 6 轮旧对话一批，实际请求约 7700 令牌，Ollama 当前只开了 4096 的上下文，直接报 `BadRequestError`。这种失败不算“输出被截断”，不会减半，这个会话就卡在这一批。

## 2. 现状（已核实）

- **Ollama 的窗口**：模型本身支持 262144（`/api/show`），但 Ollama 加载时只开 4096（`/api/ps` 的 `context_length`）。OpenAI 兼容接口**不能按请求放大**：`extra_body` 带 `options.num_ctx` 或 `num_ctx` 都照样被拒。要放大只能在启动 Ollama 时设环境变量 `OLLAMA_CONTEXT_LENGTH`，或者另建一个 Modelfile。
- **代码里没有“摘要模型窗口”这个概念**：`LLMCompressor` 的输入上限写死 8192（估算值），选批次的预算写死 6000（`ConversationMemory(summary_token_budget=6000)`）。远端档案虽然有 `context_tokens`，摘要也不用它（D-103 定的）。
- **令牌估算偏低**：同一段提示，本机估算和 Ollama 实际计数对比：

  | 批次 | 估算（整段提示） | 实际 | 实际 / 估算 |
  |---|---|---|---|
  | 1 轮 | 1184 | 1490 | 1.26 |
  | 4 轮 | 2569 | 3448 | 1.34 |

  所以估算 5999 的 6 轮批次能通过 8192 的检查，实际却要约 8000。
- **输入超过 8192 时**：`LLMCompressor` 抛 `ValueError('summary input exceeds token budget')`，也不会减半，同样卡住。

## 3. 设计

### 3.1 摘要模型的窗口（第 8 节第 1、2 问）

- 新设置 `SUMMARY_MODEL_CONTEXT_TOKENS`，默认 **4096**（Ollama 不设环境变量时的值），只给本机路径用；档案路径用档案的 `context_tokens`。
- 由窗口算出三个数，取代现在写死的值：
  - 输出上限 = `min(1536, 窗口 // 4)`。窗口 4096 时是 1024，够 4 轮的摘要（实测 699）；窗口 8192 以上仍是 1536；
  - 输入上限（估算）= `(窗口 - 输出上限 - 64) / 1.5`。1.5 是估算偏低的余量（实测 1.26～1.34）；
  - 选批次的预算 = `min(6000, 输入上限 - 600)`。600 留给系统提示、小标题和 JSON 外框。
  窗口 4096 时，输入上限约 2000、批次预算约 1400，一批大约 1～2 轮；窗口 16384 时批次预算回到 6000。
- `.env.example` 写明：想一批多摘几轮，就在启动 Ollama 前设 `OLLAMA_CONTEXT_LENGTH=16384`，同时把 `SUMMARY_MODEL_CONTEXT_TOKENS` 设成一样的值。两边不一致时以小的为准，否则还会被拒。
- `--show-limits` 的“单次摘要输出上限”一行改成按窗口算出来的值，并多一行“摘要模型窗口”。

### 3.2 批次太大也减半（第 8 节第 3 问）

- `LLMCompressor` 的输入超过上限时，改抛 `SummaryInputTooLargeError`。它和 `SummaryTooLongError` 共用一个父类 `SummaryBatchTooLargeError`（仍是 `RuntimeError` 的子类）。
- `ConversationMemory` 收到这个父类就走 D-104 的路子：批次多于 1 轮减半，只有 1 轮就跳过。
- 服务商返回的 `BadRequestError` 不按错误文字猜原因，照旧试 2 次后放弃。有了 3.1 的预算，正常不会再走到这一步。

### 3.3 摘要过长（第 8 节第 4、5 问）

- **提示词**：用户消息末尾多一句（中英文对话都加）：`Be concise: keep decisions, facts and open questions; do not copy data rows, records, logs or code verbatim.` 系统提示和小标题不变。
- **宿主检查**：摘要的估算令牌数超过 `max(400, 0.5 × 这一批对话的估算令牌数)` 时，`LLMCompressor` 抛 `SummaryNotShorterError`，摘要正文放在异常的 `summary` 属性里。400 的下限是给很短的批次留的：六个小标题本身就要几十个令牌。实测正常摘要的比例是 0.14～0.15，过长的是 0.93，0.5 的门槛离两边都远。
- **处理**：`ConversationMemory` 第一次收到时照常失败，调度器约 1 秒后重试同一批；同一批**第二次**还是过长，就收下异常里带的那份摘要，记一条警告（只有类型名）。这样不会因为摘要偏长丢掉内容，也不会卡住；最多多花一次调用。
- 这个错误不减半、不跳过。

### 3.4 要改的文件

- 修改：
  - `app/config.py`、`.env.example`：`SUMMARY_MODEL_CONTEXT_TOKENS`；
  - `app/services/memory_compression/llm_compressor.py`：按窗口算上限、三个异常类、提示词那一句、长度检查；
  - `app/core/dependencies.py`：打开压缩器时把窗口传进去（本机用设置，档案用 `context_tokens`）；
  - `app/models/conversation_memory.py`：批次预算从压缩器取，处理两个新异常；
  - `app/agent/run_limits.py`：两行限额。
- 新增：`tests/test_summary_fit.py`。
- 不改：调度器、归档格式、摘要进上下文的预算、`SYSTEM_PROMPT`、小标题、模型档案的结构、黄金对照文件、依赖；不自动改 Ollama 的配置。

## 4. 测试（离线）

- 窗口 4096、8192、16384、262144 时三个数各是多少；没设新设置时用 4096；档案路径用 `context_tokens`。
- 估算超过输入上限 → `SummaryInputTooLargeError` → 多轮批次减半、单轮跳过（复用 D-104 的测试写法）。
- 提示词那一句：中文、英文批次都有，在中文那句之前；其余部分不变。
- 过长：假客户端第一次、第二次都返回很长的摘要 → 第一次失败，第二次收下并提交；只有第一次过长 → 第二次正常摘要被提交；短批次（估算 100 令牌）返回 300 令牌的摘要不算过长。
- 日志只有类型名。
- D-104 的测试、`tests/test_summary_scheduler.py`、`tests/agent/test_summary_lifecycle_e2e.py` 现有断言尽量不改；因为默认窗口变成 4096 而必须改的断言，报告里逐个列出。

## 5. 不做的

- 自动读 Ollama 实际开的窗口：`/api/ps` 只在模型已加载时才有，而且是 Ollama 专用接口；
- 自动改 Ollama 的环境变量或建 Modelfile；
- 按服务商错误文字判断“超窗口”；
- 改估算算法：用 1.5 的余量兜住，不去碰全项目共用的估算；
- 把过长的摘要再压一次。

## 6. 风险

- **默认窗口 4096 让批次变小**：一批只有 1～2 轮，摘要条数变多、每条覆盖的轮数变少。网页上每个任务本来就一轮一批，影响不大；旧对话会多花几次调用（本机不花钱）。
- **1.5 的余量是按两组测量定的**：代码或大段英文的估算偏差可能不同。真超了会被 Ollama 拒绝，照旧试 2 次后放弃，不会比现在更坏。
- **过长时多一次调用**：只在第一次过长时发生。
- **提示词那一句会改变所有摘要请求**：包括英文对话（D-104 那句只加在中文对话上）。黄金对照文件里没有摘要请求，不受影响。
- **改完要再实测**：用本机 Ollama 跑同样的脚本：6 轮旧对话不再被拒、单轮摘要不再写到 1380 令牌。不花钱，但要你同意再跑。

## 7. 实施

照旧由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写实施说明并核对，Gemini 结束前清理无关文件并前台跑 `init.ps1`；离线通过后，经你同意用本机 Ollama 复测。

## 8. 要用户定的

1. **新设置**：推荐**加 `SUMMARY_MODEL_CONTEXT_TOKENS`，默认 4096**，档案路径用 `context_tokens`。
2. **由窗口算上限**：推荐**输出 `min(1536, 窗口//4)`、输入按 1.5 倍余量、批次预算 `min(6000, 输入上限-600)`**。
3. **批次太大**：推荐**输入超限也按 D-104 减半或跳过**；服务商的 400 不按文字猜。
4. **摘要过长的门槛**：推荐**超过 `max(400, 0.5 × 输入)` 算过长**，提示词加一句“不要逐条抄数据”。
5. **过长的处理**：推荐**第一次重试，第二次还过长就收下**。另一个做法是第二次也拒绝、按单轮跳过，代价是丢掉这一轮的摘要。
6. **你那边**：要一批多摘几轮，就在启动 Ollama 前设 `OLLAMA_CONTEXT_LENGTH=16384`，并在 `.env` 里写同样的 `SUMMARY_MODEL_CONTEXT_TOKENS`。可选，不设也能正常工作。我不会改你的 `.env` 和 Ollama 设置。
7. **实施和复测**：推荐 Gemini 实施、Claude 核对，离线通过后用本机 Ollama 复测。
8. **记录**：推荐新开 **D-105**。

## 9. 实施记录

日期：2026-11-25。按第 8 节推荐项与实施说明完成离线代码与测试改动（未连接本机 Ollama 或远端模型，未改动 `docs/DECISIONS.md`、`app/services/rag_agent_service.py`、`app/services/summary_scheduler.py`、`app/services/conversation_archive.py`、`tests/fixtures/`）：

- `app/config.py`、`.env.example`：新增设置 `summary_model_context_tokens: int = 4096`（`SUMMARY_MODEL_CONTEXT_TOKENS`），`summary_model_problem()` 增加小于 2048 的中文报错；`.env.example` 补充 Ollama 上下文窗口与 `OLLAMA_CONTEXT_LENGTH` 说明。
- `app/services/memory_compression/llm_compressor.py`：
  - 新增常量 `TOKEN_ESTIMATE_MARGIN = 1.5`、`PROMPT_OVERHEAD_TOKENS = 600`、`MAX_BATCH_TOKENS = 6000`、`MIN_SUMMARY_ALLOWANCE = 400`、`MAX_SUMMARY_RATIO = 0.5`、`CONCISE_NOTE`；
  - `LLMCompressor.__init__` 增加仅关键字参数 `context_tokens: int | None = None`（为 `None` 时行为不变，非 `None` 时要求为 ≥ 2048 的整数并推算 `_max_output_tokens`、`_max_input_tokens`），新增只读属性 `batch_token_budget`；
  - 新增 `SummaryBatchTooLargeError(RuntimeError)`、`SummaryInputTooLargeError(SummaryBatchTooLargeError)`、`SummaryNotShorterError(RuntimeError)`，`SummaryTooLongError` 改为继承 `SummaryBatchTooLargeError`；输入估算超限改抛 `SummaryInputTooLargeError`；
  - `_build_prompt` 在小标题列表后始终追加 `CONCISE_NOTE`，再按 D-104 规则追加 `CHINESE_BODY_NOTE`；`compress` 在小标题检查通过后检查摘要估算令牌数是否超过 `max(400, 0.5 * source_tokens)`，超过则抛 `SummaryNotShorterError(summary.strip())`（异常消息不含摘要原文）。
- `app/core/dependencies.py`：模块级 `conversation_compressor` 与 `open_conversation_compressor` 本机分支传入 `context_tokens=config.summary_model_context_tokens`（或传入 `settings` 的同名字段），档案分支传入 `context_tokens=profile.context_tokens`。
- `app/models/conversation_memory.py`：`__init__` 通过 `getattr(compressor, "batch_token_budget", None)` 收紧 `_summary_token_budget`；`_compress_archive` 对 `SummaryBatchTooLargeError` 走减半或单轮跳过逻辑；新增 `_long_summary_batch` 状态，对同一批次连续第二次 `SummaryNotShorterError` 接受 `exc.summary` 提交并仅以类型名记警告，不减半、不跳过，提交以及 `clear()` / `restore()` 时复位为 `None`。
- `app/agent/run_limits.py`：`RunLimits` 新增 `summary_context_tokens` 字段，`rows()` 在“历史压缩模型”之后新增“摘要模型窗口（SUMMARY_MODEL_CONTEXT_TOKENS 或档案）”行，并将“单次摘要输出上限”改为按窗口推算的值。
- `tests/test_summary_fit.py`：新增 22 项离线测试（13 个测试函数，含参数化用例），覆盖 4096/8192/16384/262144 窗口推算、`context_tokens=None` 与非法值、配置校验、本机与档案打开压缩器、批次预算收紧、输入超限减半与跳过、提示词顺序、摘要过长三种情形与调度器重试、日志脱敏及 `--show-limits`。
- 现有测试断言同步更新：
  - `tests/test_summary_quality.py`：英文提示词断言补入 `CONCISE_NOTE`；输入预算检查断言由 `ValueError` 更新为 `SummaryInputTooLargeError`；`--show-limits` 行位置和默认值同步为窗口 `4096 令牌` 与输出上限 `1024 令牌`。
  - `tests/test_summarizer_role.py`：档案 `context_tokens=64000` 下 `comp._max_input_tokens` 断言由 `8192` 更新为 `41600`（并断言 `batch_token_budget == 6000`）。
  - `tests/test_summary_policy.py`：`test_compressor_rejects_input_over_budget_without_model_call` 断言由 `ValueError` 更新为 `SummaryInputTooLargeError`。
- Claude 核对（2026-11-26）：
  - 逐个看过设置、压缩器、打开函数、对话记忆、限额表和五处现有测试断言的改动，没有改动；五处断言都是因为默认窗口变成 4096、提示词多一句、输入超限改抛新异常而必须改的；
  - 自己重跑 `init.ps1`：2483 passed、28 skipped（基线 2461，新增 22 个），`tests/fixtures/` 没有变化，改过的文件都是 LF；你没提交的文件没有被碰过，`data/` 没有新东西。

## 10. 本机复测（2026-11-26，用户授权）

本机 Ollama 0.40.16，`qwen3:4b-instruct`，Ollama 仍是默认的 4096 窗口，`SUMMARY_MODEL_CONTEXT_TOKENS` 没设（默认 4096）；只用编造的对话，脚本和临时目录都在仓库外，跑完已删除。

| 测什么 | D-104 时 | 这次 |
|---|---|---|
| `--show-limits` | — | 窗口 4096 令牌、单次输出上限 1024 令牌 |
| 直接摘要 4 轮（估算约 2570） | 511 令牌，中文 | 发送前就以 `SummaryInputTooLargeError` 拒绝，不发给模型（4 轮放不进 4096 的窗口，符合设计） |
| 网页链路，每轮一个任务，跑 3 次 | 3 次里 2 次约 1380 令牌 | 3 次都是 188～199 令牌，中文，小标题通过 |
| 网页链路，没有任务编号的旧对话 | 6 轮一批被 Ollama 拒绝 | 按批次预算一批 1 轮，172 令牌，提交成功 |

另外，测试包装一开始没有转发 `batch_token_budget`，旧对话那次选出了 6 轮一批，结果依次减半为 3 轮、1 轮后摘要成功，顺带实测了“输入超限也减半”的兜底。“过长后第二次收下”这条路这次没有被触发（摘要都很短），只有离线测试覆盖。
