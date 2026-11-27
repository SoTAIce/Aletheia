# 中转服务换模型：让提示有用，必要时停下（方案）

日期：2026-11-26。状态：**已实施**（D-107）。第 7 节六条按推荐定（用户 2026-11-26）。记为 D-107（D-106 还没提交）。

来源：2026-11-26 后台摘要端到端复测（`docs/summary_stall_plan.md` 第 10 节）。主模型两次都答非所问，两次运行都带 D-079 的提示“服务商回答所用的模型和请求的不同”，任务仍被判为“完成”。

下面的现状按当前代码（`2298d44` 加未提交的 D-106）和仓库里已有的本地轨迹核实过，没有调用任何模型。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有你自己没提交的改动。

## 1. 现在怎么做（D-079）

- 每次模型调用，适配器把请求的模型名和响应里的 `model` 字段交给计量器（`openai_compatible_tool_calling_model.py:269-272`）。
- `provider_state.different_model` 只比名字：同名、带日期或版本后缀、带服务商前缀的算同一个，其他都算不同。
- 有一次不同，`summary()` 就把 `model_mismatch` 设成真，命令行和网页各显示一行提示。只提示、不拦截，任务状态不受影响。评测每行记 `model_mismatch`。

## 2. 查到的事实

1. **这个中转服务每次都换**：仓库里已有的轨迹共 630 次调用（`output/traces-*`、`data/` 下的审计），请求的都是 `gpt-5.6-luna`，响应报告的**全部是 `gpt-6.1-sol`**；最近一次评测 `output/bench-tune-loop-5.json` 11 次运行有 11 次带 `model_mismatch`。
2. **所以现在的提示没有用**：每次都出现，好结果和坏结果都带着它。第一次端到端答对了，复测两次答错了，看到的提示一样。
3. **报告的名字证明不了什么**：`model` 字段是中转服务自己填的，想换也可以照填原名。客户端只能看到它“自称”是谁。
4. **复测里第一次答错的具体样子**：回答是 `{"timezone":"UTC"}`，这正是 `get_current_time` 工具的参数（`app/tools/current_time_tool.py`）。也就是说，模型想调用工具，却把参数当成普通文字发了回来；我们的循环把纯文字当最终答案，于是判为“完成”。第二次回答是一段说找不到 `bash` 工具的 JSON，这个仓库没有叫 `bash` 的工具，像是被换上的模型按别处的工具习惯在回答。
5. **结果好坏更像跟着令牌档位走**：同一份评测里，第一次调用是 `heavy` 档（中转服务加了大量隐藏内容）的 8 次过了 7 次，`plain` 档的 2 次过了 1 次。样本太少，只能说明值得继续分档统计，不能下结论。

## 3. 设计

### 3.1 认可的替换模型（第 7 节第 1 问）

- 模型档案新增 `accepted_models`：一组你认可的响应模型名。`models.toml` 里每个档案可以写，默认档案用新设置 `AGENT_ACCEPTED_MODELS`（逗号分隔，默认空）。
- 响应名在这个列表里，就不算“不同”，不提示。比如你写上 `gpt-6.1-sol`，平时就安静了；哪天中转服务换成别的名字，提示才会出现，这时提示才有意义。
- 比较规则和 `different_model` 一样（后缀、前缀算同一个）。
- 列表为空时行为和现在完全一样。

### 3.2 提示写明是哪两个模型

- `summary()` 多记 `response_names`：这次运行里出现过的、不在认可列表里的响应模型名，去重，最多 3 个，每个最多 60 个字符。只是名字，不含请求或回答原文（D-073）。
- 提示改成：“注意：请求的是 gpt-5.6-luna，服务商用 gpt-6.1-sol 回答，本次结果可能不可靠。”
- 用量、轨迹、评测记录照旧；评测汇总按响应模型名多一组计数。

### 3.3 不认可的模型可以直接停下（第 7 节第 2 问）

- 模型档案新增 `on_model_mismatch`，取值 `warn`（默认，和现在一样）或 `stop`；默认档案用设置 `AGENT_ON_MODEL_MISMATCH`。
- 设成 `stop` 时：一次响应报告了不在认可列表里的名字，适配器记下这次用量后抛 `ModelSubstitutedError`，**不解析、不执行**这次回答里的工具调用，不重试。任务失败，失败类型新增 `model_substituted`，说明文字：“服务商用请求之外的模型回答（只看名字），按设置停止了任务。可以在模型档案的 accepted_models 里认可这个模型，或者换一个服务商。”
- 响应没有报告模型名时不算不同（和现在一样），也不停。
- 后台摘要、路由、团队成员各用自己的档案，各按自己的设置处理。

### 3.4 把工具参数当成答案的回复：提醒一次（第 7 节第 3 问）

- 单任务循环收到纯文字回复时，宿主先看一眼：整段文字能解析成一个 JSON 对象，非空，而且所有键都是**这次可用的某一个工具**的参数名（例如 `{"timezone":"UTC"}` 对 `get_current_time`）。
- 满足时不当最终答案，追加一条宿主提醒：“HOST: your reply looks like the arguments of tool get_current_time. Call the tool, or reply with the final answer.”，再走一轮。同一个任务只提醒一次，第二次照样收下作为答案。
- 任务目标要求只输出 JSON 时，这个检查也照样做，但只要有一个键不是工具参数就不触发；误判时多花一轮。
- 这条和换模型没有直接关系，任何模型都可能这样，但这次复测正是它让一个错误回答被判为“完成”。

### 3.5 要改的文件

- `app/models/model_profile.py`：`accepted_models`、`on_model_mismatch` 两个字段和校验；`models.example.toml` 加注释示例。
- `app/config.py`、`.env.example`：`AGENT_ACCEPTED_MODELS`、`AGENT_ON_MODEL_MISMATCH`。
- `app/agent/provider_state.py`：认可列表参与比较；`response_names`；新提示文字。
- `app/agent/run_meter.py`：把认可列表传给 `summary()`（按每次调用的档案）。
- `app/agent/openai_compatible_tool_calling_model.py`：`stop` 时抛 `ModelSubstitutedError`。
- `app/agent/runtime.py`：失败类型 `model_substituted`。
- `app/agent/agent_loop.py`：3.4 的一次提醒。
- `app/agent/run_limits.py`：`--show-limits` 多两行“认可的替换模型”“换模型时”。
- `bench/run_bench.py`：汇总按响应模型名计数。
- 新增 `tests/test_model_substitution.py`。
- 不改：请求内容（`tests/test_model_profile.py` 的逐字节对照不变）、令牌档位阈值、黄金对照文件、依赖、默认模型。

## 4. 测试（离线）

- 认可列表：响应名在列表里不提示；不在列表里提示，提示里有两个名字；列表为空和现在一样。
- `stop`：假客户端返回别的名字，抛 `ModelSubstitutedError`，用量已记，工具没执行，任务失败类型是 `model_substituted`；响应没有名字时不停。
- 名字过长、过多时截断和去重。
- 3.4：`{"timezone":"UTC"}` 触发一次提醒，模型接着调用工具后正常完成；第二次还是这样就收下；带一个非参数键的 JSON 不触发；没有工具时不触发。
- 现有的 `tests/test_provider_state.py`、评测和循环测试：断言尽量不改，必须改的逐个说明原因。

## 5. 不做的

- 用探针问模型“你是谁”：回答本身就不可信。
- 读取或比对中转服务加的隐藏内容（D-079 已否）。
- 自动换服务商或直连对照（D-079 时“先不做直连对照”）。
- 按令牌档位提示：直连服务正常就落在 `plain` 档，不能当成异常；先靠评测分档数据。

## 6. 风险

- **认可列表会让你看不到“同名但内容变了”**：名字本来就证明不了什么，这只是让提示只在名字变化时出现。
- **`stop` 在这个中转服务上会让每个任务都失败**，除非先把 `gpt-6.1-sol` 写进认可列表。所以默认是 `warn`。
- **3.4 可能误判**：一个恰好只含工具参数名的 JSON 答案会多花一轮。

## 7. 要用户定的

1. **认可列表**：推荐**加 `accepted_models`**，并由你决定要不要在自己的 `.env` 或 `models.toml` 里写上 `gpt-6.1-sol`（这两个文件我不改）。
2. **不认可时**：推荐**加 `on_model_mismatch`，默认 `warn`**，需要时你自己改成 `stop`。
3. **工具参数当答案**：推荐**提醒一次**。
4. **提示写明模型名**：推荐**写**。
5. **实施**：推荐照旧由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写说明并核对，结束前清理无关文件并前台跑 `init.ps1`。只做离线测试，不调用模型。
6. **记录**：推荐新开 **D-107**；D-106 和它可以分两次提交。

## 8. 实施记录（2026-11-26）

- `app/models/model_profile.py`、`app/config.py`：新增 `accepted_models`（最多 20 项、每项去空白且 1–128 字符，文件列表转元组）与 `on_model_mismatch`（`'warn'` / `'stop'`），两者均不进 `INHERITED`；默认档案从 `AGENT_ACCEPTED_MODELS` 与 `AGENT_ON_MODEL_MISMATCH` 读取并校验；发出的请求逐字节不变。
- `app/agent/provider_state.py`、`app/agent/run_meter.py`：新增 `substituted(requested, reported, accepted=())`；`RunMeter` 平行记录 `accepted_models` 并传给 `summary()`；`summary()` 新增去重、截断（最多 3 个、每个最多 60 字符）的 `requested_names` 与 `response_names`；有名字时提示为“注意：请求的是 A，服务商用 B 回答，本次结果可能不可靠。”，缺名字时退回旧提示。
- `app/agent/tool_calling_model.py`、`app/agent/openai_compatible_tool_calling_model.py`、`app/agent/runtime.py`、`app/agent/loop_run.py`：新增不可重试的 `ModelSubstitutedError`（消息 `provider answered with another model`）；`on_model_mismatch == 'stop'` 且命中替换时在记账后、解析前抛出，不解析也不执行工具；任务失败类型为 `model_substituted`，使用第 3.3 节中文说明。
- `app/agent/agent_loop.py`：单任务循环在收到可解析为非空 JSON 对象的纯文字回复且全部键属于某一个当前可用非控制工具参数时，提醒一次（不计 `format_repair_count`），第二次照常收下。
- `app/agent/run_limits.py`、`bench/run_bench.py`、`.env.example`、`models.example.toml`：`--show-limits` 在“单次调用”下增加“认可的替换模型”和“换模型时”两行；评测每行新增 `response_names`，报告汇总新增 `response_model_counts`；补充示例注释。
- 测试：新增 `tests/test_model_substitution.py`（32 项离线测试）；更新 `tests/test_provider_state.py` 中 1 处旧提示文字断言。离线验证通过 `init.ps1`（未调用真实模型或外部服务）。
- Claude 核对（2026-11-26）：
  - 逐个看过档案、设置、比较、计量器、适配器、失败分类、循环提醒、限额表和评测的改动，没有改动；
  - `ModelSubstitutedError` 继承 `ModelResponseError` 但不带 `retryable`，适配器和 `LoopRun` 都不会重试；它在记账之后、解析回复之前抛出，回复里的工具调用不会执行；
  - 和说明有一处小差别，接受：用量里的 `provider` 现在总是多带 `requested_names`、`response_names` 两个列表（没有替换时为空）。其他字段和提示在列表为空、设置为 `warn` 时与原来相同；
  - 改了 1 条现有断言（`tests/test_provider_state.py`，提示文字按设计变了）；自己重跑 `init.ps1`：2535 passed、28 skipped（基线 2503，新增 32 个）；`tests/fixtures/` 没有变化，改过的文件都是 LF；D-106 的改动、你没提交的文件、`data/`、`output/` 都没有变化；
  - 没有调用模型或服务。
