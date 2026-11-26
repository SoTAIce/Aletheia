# 后台摘要：`summarizer` 档案角色（方案）

日期：2026-11-25。状态：**已实施，未用真实模型验证**（D-103）。第 8 节七条按推荐定（用户 2026-11-25）。

来源：`docs/history_compression_plan.md` 第 6 节暂缓的那一项（想法出自用户的 `docs/agent_feature_ideas.md` 7.3 节）。D-100 已经让命令行和网页都能开历史压缩，但摘要模型只能是本机回环地址（3.3 节的限制）。这次让 `models.toml` 可以给后台摘要指定一个档案，用远端的便宜模型做摘要；不写时行为逐字不变，仍用本机 Ollama。

下面的现状按当前代码（`9c09895`）核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有你自己没提交的改动。

## 1. 要解决什么

- **只能用本机模型**：Ollama 没开、机器显卡紧张、或想要质量更好的摘要时，没有别的选择。设置 `SUMMARY_MODEL_BASE_URL` 指向远端会被启动拒绝（D-100 有意这样做，留给本角色放开）。
- **摘要的密钥和地址不走档案**：主循环、路由、成员、调研都在 `models.toml` 里配，只有摘要单独写死密钥 `ollama`，换服务商没有地方写密钥。
- **以后要复用**：设计审查 3.1 节的工作摘要、3.4 节的记忆抽取都需要“便宜模型”这个角色。这次只接后台摘要，但角色先定下来。

## 2. 现状（已核实）

- **角色**：`app/models/model_profile.py:87` 的 `ROLES = ('main', 'route', 'member', 'explorer')`。`ModelProfiles.__post_init__` 要求角色正好是这四个；`load_profiles` 里没写的角色回落到 `main`，写了不认识的角色报错。`rag_agent_service.py:342` 按 `ROLES` 的顺序建 `RoleModels`，`used()` 决定要开哪些客户端、要校验哪些密钥。
- **摘要客户端**：`app/core/dependencies.py` 的 `open_conversation_compressor()` 用 `SUMMARY_MODEL_BASE_URL`、`SUMMARY_MODEL` 和固定密钥 `ollama` 开一个 `max_retries=0`、超时 120 秒的客户端，交给 `LLMCompressor`。
- **打开和检查**：`app/agent/__main__.py:623` 的 `open_history_compressor(enabled, stack)` 是命令行和网页共用的入口：先 `summary_model_problem()`（只许回环），再开客户端，再用 `check_conversation_compressor` 列一次模型（3 秒）。
- **摘要请求**：`LLMCompressor.compress` 发 `model`、两条消息、`temperature=0.1`、`max_tokens=512`，输入上限 8192；不发 `enable_thinking`。用量记进进程账本 `record_auxiliary("summary", ..., in_request=False)`。
- **钩子和预算**：D-080 记下后台摘要不走 `HookedModel`、不计入令牌上限、不进审计。
- **限额表**：`run_limits.py:120` 的“后台摘要”一行写的是“本机 `<模型>`”。

## 3. 设计

### 3.1 角色写法（第 8 节第 1、2 问）

- `models.toml` 的 `[roles]` 可以多写一项 `summarizer = "<档案名>"`。
- **它是可选角色，不回落到 `main`**。没写就照旧用本机 Ollama（`SUMMARY_MODEL_*`）。原因：如果回落到 `main`，你只是为了路由加了一个档案文件，会话原文就会悄悄发往远端。
- 实现上不放进 `ROLES`（那四个决定了 `RoleModels` 的结构和要开的客户端），另加 `OPTIONAL_ROLES = ('summarizer',)`，`ModelProfiles` 多一个字段 `summarizer: str | None = None`。
  - `[roles]` 里认 `ROLES` 和 `OPTIONAL_ROLES`，其他照旧报错；
  - `summarizer` 指向不存在的档案时报错，档案的密钥变量是空的也报错（和四个主角色一样）；
  - `used()` 和 `describe()` 不变，所以主循环那边要开的客户端和 `--show-limits` 的角色表都不受影响。

### 3.2 摘要用档案里的哪些字段（第 8 节第 3 问）

- 用：`base_url`、`api_key_env`（读密钥）、`model`、`timeout_seconds`（客户端超时；调度器单次 30 秒照旧是外层时限）。
- `enable_thinking`：档案写了才发，经 `extra_body` 传给服务商（DashScope 的 qwen3 非流式调用通常要关思考）。没写不发，和现在一样。
- 不用：`context_tokens`、`max_output_tokens`、`temperature`、`json_mode`、`prompt_cache_key`。摘要照旧是输入 8192、输出 512、温度 0.1，提示词和小标题检查不变。这样换模型只换“谁来写”，不换“写什么、写多长”。
- 价格字段：档案写了价格时，`--show-limits` 不估算；进程账本照旧只记令牌数（不新增花费计算）。

### 3.3 远端地址的提示（第 8 节第 4 问）

- 档案地址不是回环地址时，启动打印一行：`历史压缩：会话原文会发往 <主机>（档案 <名字>）`。只写主机，不写路径和密钥。
- 不要求额外的确认参数：在 `models.toml` 里写 `summarizer` 本身就是明确的选择，而且还要开 `--compress-history` 才会生效。
- `SUMMARY_MODEL_BASE_URL` 的回环限制**保持不变**：远端只能通过档案角色走，`.env` 那两项仍只给本机 Ollama 用。

### 3.4 打开、检查和状态（第 8 节第 5 问）

- `open_history_compressor` 改为：
  1. 没开 `--compress-history`：`off`，档案文件里的 `summarizer` 不读不用；
  2. 档案写了 `summarizer`：用档案开客户端（密钥从档案的变量读），跳过 `summary_model_problem()`；`.env` 里明确写了 `SUMMARY_MODEL_*` 时打印一行“已用档案 `<名字>`，`SUMMARY_MODEL_*` 这次不用”；
  3. 没写：和现在逐字相同。
- 档案文件本身有错（比如 `summarizer` 指向不存在的档案）时，命令行和网页启动照样拒绝，退出码 2，和现在档案文件出错一致。
- 可用性检查照旧列一次模型、3 秒时限，提示按来源改说法：
  - 本机：不变（“请先启动 Ollama”“请先 `ollama pull`”）；
  - 档案：“连不上摘要服务（`<主机:端口>`，`<异常类型名>`）”“摘要服务上没有模型 `<名字>`”，不提 Ollama。
  - 不可用时照旧：照常启动、这次不压缩历史、健康接口报 `unavailable`。
- 健康接口的 `history_compression` 仍只有 `off`、`on`、`unavailable` 三个值，不带地址和模型名。
- `--show-limits` 的“后台摘要”一行：本机时不变；档案时写“档案 `<名字>`，模型 `<名字>`，单次 30 秒，最多试 2 次”。

### 3.5 不变的

- D-080：摘要不走钩子、不计入每次运行的令牌上限、不进审计，换成档案模型后仍然这样。这一点写进 D-103 和 `models.example.toml` 的注释，让你知道远端摘要的花费只出现在进程账本里。
- 摘要提示词、`SummaryScheduler`、`ConversationMemory`、会话归档、网页历史显示、四个主角色、黄金对照文件、依赖。
- 没有档案文件、或档案文件没写 `summarizer` 时，所有请求逐字节不变（D-084 的约束）。

### 3.6 要改的文件

- 修改：
  - `app/models/model_profile.py`：`OPTIONAL_ROLES`、`ModelProfiles.summarizer`、`load_profiles` 认这一项并校验；
  - `app/core/dependencies.py`：`open_conversation_compressor(profile=None, settings=None)`，有档案时用档案开客户端；`check_conversation_compressor` 按来源出提示；
  - `app/services/memory_compression/llm_compressor.py`：可选的 `extra_body`，默认不传，请求逐字节不变；
  - `app/agent/__main__.py`：`open_history_compressor` 读档案的 `summarizer`、远端提示；
  - `app/agent/run_limits.py`：“后台摘要”一行；
  - `models.example.toml`：一行注释掉的 `# summarizer = "cheap"` 和说明（不回落到 main、原文会发往该地址、不计入令牌上限）。
- 新增：`tests/test_summarizer_role.py`。
- 不改：`ROLES`、`RoleModels`、`rag_agent_service.py`、`app/api/*`（网页共用 `open_history_compressor`，不用单独改）、`app/config.py`、`.env.example`、用户的 `.env`。

## 4. 测试（离线）

全部用假客户端，不连 Ollama、不连服务商：

- 没有档案文件、或档案没写 `summarizer`：压缩器的地址、密钥、模型名、超时和现在逐字相同；`tests/test_model_profiles.py`、`tests/test_history_compression_setup.py` 现有断言不变。
- 写了 `summarizer`：压缩器用档案的地址、密钥、模型名、超时；`roles` 仍是四个主角色，`used()` 不含只给摘要用的档案。
- 写了 `summarizer` 但档案不存在、密钥变量为空：`ModelProfileError`，中文说明；写了别的未知角色仍报错。
- 档案写了 `enable_thinking = false`：请求里带 `extra_body`；没写：请求参数和现在逐字相同。
- 远端地址：启动打印主机提示，不含路径和密钥；回环地址的档案不打印。
- 没开 `--compress-history`：不读 `summarizer`，状态 `off`。
- 检查提示：档案来源的提示不提 Ollama，只有主机、端口和异常类型名。
- `--show-limits`：两种来源各一行；不出现地址。
- 网页：开了压缩、档案写了 `summarizer` 时 `RagAgentService` 收到的压缩器用档案模型（复用 D-100 的网页测试替身）。

## 5. 不做的

- 工作摘要（审查 3.1）、自动记忆抽取（审查 3.4）、引用核对改用这个角色：各自写方案时再接；
- 摘要计入每次运行的令牌上限或走钩子（D-080 不改）；
- 摘要失败时从档案回落到本机 Ollama（两条路各管各的，不悄悄换地方发原文）；
- 网页上切换摘要模型；
- 远端摘要的花费估算。

## 6. 风险

- **会话原文出本机**：写了远端 `summarizer` 后，每次压缩都会把最近若干轮的原文发给那个服务商。只有你写了这一项并开了 `--compress-history` 才会发生，启动有一行提示。
- **中转服务不支持 `/models`**：有的中转地址没有列模型的接口，检查会报“连不上”，这次就不压缩。如果你要用的服务是这样，第 8 节第 5 问可以改成“档案来源跳过检查”。
- **小标题检查更严格的模型**：远端模型可能不按六个英文小标题输出，摘要会被判失败、重试后放弃（和本机模型一样只记日志）。没有用真实模型验证。
- **没用真实模型验证**：全部离线测试；要试的话需要你同意后开一次 `--compress-history` 跑几轮对话。

## 7. 实施

照旧由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写实施说明并核对，Gemini 结束前清理无关文件并前台跑 `init.ps1`；只做离线测试。

## 8. 要用户定的

1. **角色名和位置**：推荐 `models.toml` 的 `[roles]` 加 `summarizer`，不放进四个主角色。
2. **没写时**：推荐**照旧用本机 Ollama，不回落到 `main`**。
3. **用档案的哪些字段**：推荐只用地址、密钥、模型名、超时和 `enable_thinking`；摘要的长度、温度、提示词不变。
4. **远端提示**：推荐**启动打印一行主机名，不另加确认参数**；`SUMMARY_MODEL_BASE_URL` 仍只许回环。
5. **可用性检查**：推荐**档案来源也列一次模型（3 秒）**，不可用就这次不压缩；另一个做法是档案来源跳过检查（适合不支持 `/models` 的中转）。
6. **实施**：推荐照旧由 Gemini 实施、Claude 核对，结束前清理无关文件；只做离线测试。
7. **记录**：推荐新开 **D-103**。

## 9. 实施记录

日期：2026-11-25，基于提交 `9c09895` 离线实施并验证。

- **模型档案**（`app/models/model_profile.py`）：新增 `OPTIONAL_ROLES = ('summarizer',)`、`ModelProfiles.summarizer: str | None = None` 与 `summarizer_profile()`；`load_profiles` 识别 `summarizer`（不回落到 `main`）并校验指向的档案存在且密钥非空；`ROLES`、`used()`、`describe()` 保持不变。
- **摘要客户端与探测**（`app/services/memory_compression/llm_compressor.py`、`app/core/dependencies.py`）：`LLMCompressor.__init__` 增加仅关键字参数 `extra_body: dict | None = None`，仅非空时传入 `create(...)`；`open_conversation_compressor(profile=None, settings=None)` 在有档案时按档案的地址、密钥、模型、超时及可选 `enable_thinking` 打开客户端；`check_conversation_compressor` 新增 `source='local'|'profile'`，档案来源提示不提及 Ollama 且只输出主机端口与异常类型名。
- **命令行、网页与限额表**（`app/agent/__main__.py`、`app/api/__main__.py`、`app/agent/run_limits.py`）：`open_history_compressor` 在关闭时不读档案，有 `summarizer` 时跳过回环校验、按需输出 `SUMMARY_MODEL_*` 覆盖提示与非回环主机提示；CLI 与网页入口均捕获 `ModelProfileError` 并以退出码 2 拒绝启动；`RunLimits` 增加 `summary_profile`，`--show-limits` 在档案来源时显示 `档案 <档案名>，模型 <模型名>，单次 30 秒，最多试 2 次`。
- **模板与测试**（`models.example.toml`、`tests/test_summarizer_role.py`）：模板 `[roles]` 末尾添加注释掉的 `# summarizer = "cheap"` 及英文说明；新增 16 项离线测试覆盖角色解析、校验、`extra_body`、探测文案、启停分支、提示、退出码 2、`--show-limits` 与网页接入，现有测试断言未修改。
- **验证**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1` 通过，结果 `2451 passed, 28 skipped in 95.56s (0:01:35)`（离线验证，未连接真实模型或本机 Ollama）。
- Claude 核对（2026-11-25）：
  - 逐个看过档案角色、`extra_body`、打开函数和检查提示、两个入口的 `ModelProfileError`、限额表和模板的改动，没有改动；
  - 没写 `summarizer` 时本机那条路径的代码逐字保留，现有测试断言一条没改；
  - 自己重跑 `init.ps1`：2451 passed、28 skipped（基线 2435，新增 16 个），`tests/fixtures/` 没有变化，改过的文件都是 LF；
  - 你没提交的文件没有被碰过（按校验值核对）；`data/` 下没有新文件，没有遗留的无关文件；
  - 没有连接 Ollama 或服务商，没有调用真实模型。
