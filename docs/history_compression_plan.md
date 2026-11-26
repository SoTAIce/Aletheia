# 后台摘要：网页接上历史压缩、可用性检查、模型设置（方案）

日期：2026-11-25。状态：**已实施，未用真实 Ollama 验证**（D-100）。第 8 节八条按推荐定（用户 2026-11-25）。由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写实施说明并核对，Gemini 结束前清理无关文件。

来源：3.5 排序第 3 项原来是“新的模型角色（后台摘要）”（`docs/batch_run_plan.md` 第 1 节，想法出自用户的 `docs/agent_feature_ideas.md` 7.3 节）。第一版方案要加 `summarizer` 档案角色；用户 2026-11-25 说明本机已经装了 Ollama，于是改成三件更实在的事：

1. 网页后端也能开历史压缩；
2. 开了历史压缩时先检查本机摘要模型能不能用（设计审查 3.5 节的建议）；
3. 摘要模型的地址和名字从代码里挪进 `.env` 设置，默认值不变。

`summarizer` 档案角色暂缓（第 6 节）。下面的现状按当前代码（`506ae71`）核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有用户自己没提交的改动。

## 1. 要解决什么

- **网页没有历史压缩**：后台摘要只有命令行加 `--compress-history` 才开。网页聊久了，会话历史只能在请求放不下时从最老的一轮整段删掉。
- **摘要模型不可用时没人知道**：Ollama 没启动、模型没拉下来时，摘要在后台失败、重试两次后放弃，只记一条日志警告，用户看到的是“历史越来越长”或“早期内容被删”，不知道为什么。
- **摘要模型写死在代码里**：换一个本机模型（比如更大的 `qwen3`）要改代码。

## 2. 现状（已核实）

- **摘要模型**：`app/core/dependencies.py` 写死 `http://127.0.0.1:11434/v1`、密钥 `ollama`、模型 `qwen3:4b-instruct`；`open_conversation_compressor()` 开一个 `max_retries=0` 的客户端（重试交给调度器），超时 120 秒。
- **命令行**：`app/agent/__main__.py` 的 `open_cli_service` 在 `--compress-history` 时要求 `CONVERSATION_STORAGE_DIR`，再把压缩器交给 `RagAgentService(conversation_compressor=...)`。
- **网页**：`app/api/__main__.py` 建 `RagAgentService` 时没传 `conversation_compressor`，也没有对应参数。但服务本身已经有调度器（`rag_agent_service.py:245` 的 `SummaryScheduler`）、按用户隔离的归档（`_archive_root`），并且每次加载会话时都会补摘要（`_session_conversation`，第 690 行）。所以网页只差“把压缩器传进去”。
- **网页历史显示**：`session_turns`（第 974 行）有归档时从归档读原文，不受摘要影响；页面上看到的历史不会变成摘要。
- **调度器**：`app/services/summary_scheduler.py`：最多并发 2、排队 64、单次 30 秒、每个工作最多试 2 次；失败只记异常类型名（D-033）。
- **网页健康接口**：`GET /api/health` 已经报告权限规则条数、用户 MCP 服务等（`app/api/app.py`）。
- **钩子和预算**：D-080 记下摘要不走 `HookedModel`、不计入令牌上限、不进审计。这次不变。

## 3. 设计

### 3.1 网页接上历史压缩（第 8 节第 1 问）

- `python -m app.api` 加 `--compress-history`，默认关，和命令行同名、同规则：
  - 要求设置了 `CONVERSATION_STORAGE_DIR`，否则启动拒绝；
  - 打开压缩器的方式和命令行相同（共用一个函数），交给 `RagAgentService`。
- 网页关闭时，服务关闭照旧等调度器收尾（`aclose`），不改。
- `GET /api/health` 多一项 `history_compression`：`off`、`on`、`unavailable`（见 3.2）。只有这三个值，不带地址和模型名。网页侧栏显示一行“历史压缩：开 / 关 / 不可用”。

### 3.2 可用性检查（第 8 节第 2、3 问）

- 只在开了 `--compress-history` 时做，命令行和网页都一样。
- 启动时用压缩器的客户端列一次模型（OpenAI 兼容接口的 `GET /models`，Ollama 支持），时限 3 秒，不发任何对话内容，不算模型调用：
  - 连不上：提示“历史压缩不可用：连不上本机摘要服务（`<主机:端口>`），请先启动 Ollama”；
  - 连上了但列表里没有这个模型：提示“历史压缩不可用：本机没有模型 `<名字>`，请先 `ollama pull <名字>`”；
  - 出错只写异常类型名（D-033）。
- 不可用时：**照常启动，这次运行关掉历史压缩**，打印上面的提示，网页健康接口报 `unavailable`。不让整个服务起不来。
- 运行中 Ollama 掉线不另外处理，照旧由调度器重试后放弃；以后需要再加。

### 3.3 模型设置挪进 `.env`（第 8 节第 4 问）

- `app/config.py` 加两个设置，默认值就是现在写死的：
  - `SUMMARY_MODEL_BASE_URL`，默认 `http://127.0.0.1:11434/v1`；
  - `SUMMARY_MODEL`，默认 `qwen3:4b-instruct`。
- `.env.example` 加两行注释说明。用户的 `.env` 不改；没写这两项时行为逐字相同。
- **地址必须是本机回环地址**（`127.0.0.1`、`::1`、`localhost`），否则启动拒绝，说明“摘要会把会话原文发出去，远端模型要等 `summarizer` 档案角色”。这样“对话原文不出本机”的保证不变。
- 密钥仍固定为 `ollama`，不加设置项。

### 3.4 要改的文件

- 修改：
  - `app/config.py`、`.env.example`：两个设置和回环校验；
  - `app/core/dependencies.py`：从设置读地址和模型名；新增 `check_conversation_compressor(compressor)`，返回 `None` 或一句中文说明；
  - `app/agent/__main__.py`：`open_cli_service` 调检查，不可用时不传压缩器并打印提示；
  - `app/api/__main__.py`：`--compress-history`，同样的打开和检查；
  - `app/api/app.py`：健康接口的 `history_compression`；
  - `app/api/static/app.js`（可能还有 `index.html`）：侧栏一行状态；
  - `app/agent/run_limits.py`：`--show-limits` 多一行“历史压缩：本机 `<模型>`，单次 30 秒，最多试 2 次”（只有模型名，不带地址）。
- 新增：`tests/test_history_compression_setup.py`。
- 不改：`SummaryScheduler`、`ConversationMemory`、`LLMCompressor`、摘要提示词、模型档案、长期记忆、黄金对照文件、依赖。

## 4. 测试（离线）

全部用假客户端，不连 Ollama：

- 没写两个新设置：压缩器的地址、模型名、超时和现在逐字相同；`tests/test_summary_scheduler.py` 现有断言不变。
- 写了：压缩器用新的地址和模型名。地址不是回环时启动拒绝，退出码 2，说明是中文。
- 检查：模型在列表里时通过；列表里没有、连接出错、超过 3 秒时返回对应的中文说明，说明里只有异常类型名。
- 命令行和网页：检查不通过时照常启动，不传压缩器，打印提示；网页健康接口报 `unavailable`；没开 `--compress-history` 时不做检查、报 `off`。
- 网页开了压缩：`RagAgentService` 收到压缩器；一个会话超过阈值后调度器会被通知（用假压缩器）；`/api/sessions/{id}/turns` 返回的仍是原文。
- 网页没开：请求、事件、健康接口之外的输出和现在逐字相同。

## 5. 不做的

- 运行中定期检查 Ollama；
- 远端摘要模型（等 `summarizer` 角色）；
- 工作摘要（审查 3.1）、长期记忆抽取改用摘要模型；
- 网页上直接开关历史压缩（只用启动参数）。

## 6. 暂缓：`summarizer` 档案角色

第一版方案的做法：`models.toml` 的 `[roles]` 可以写 `summarizer`，没写时仍用本机 Ollama、不回落到 `main`（否则加一个档案文件就会把对话原文发往远端），远端地址启动时提示。用户本机有 Ollama，暂时用不上；等要用远端便宜模型、或做工作摘要和记忆抽取时再写方案。3.3 的回环限制到时候由这个角色放开。

## 7. 风险

- **网页开压缩后多占本机资源**：Ollama 在本机跑，摘要和你的其他程序抢显卡和内存。默认关，开不开由你决定。
- **`/models` 列表的名字和请求用的名字不一致**：Ollama 的列表名带标签（`qwen3:4b-instruct`），和请求名一致；写成不带标签的名字时可能误判为“没有”。比较时按完整名字，提示里写清楚要写全名。
- **没用真实 Ollama 验证**：检查和网页接入都只做离线测试；你本机可以手动开一次 `--compress-history` 看提示是否正确（不涉及服务商，但是否跑由你决定）。

## 8. 要用户定的

1. **网页接上历史压缩**：推荐**加 `--compress-history` 启动参数，默认关**，和命令行一致。
2. **检查方式**：推荐**启动时列一次模型，3 秒时限**，不发对话内容。
3. **不可用时**：推荐**照常启动、这次关掉历史压缩并提示**。另一个做法是拒绝启动，但会让网页因为一个可选功能起不来。
4. **新设置**：推荐**加 `SUMMARY_MODEL_BASE_URL` 和 `SUMMARY_MODEL`，默认值不变，地址只许回环**。
5. **网页显示状态**：推荐**健康接口加 `history_compression`，侧栏显示一行**。
6. **`summarizer` 档案角色**：推荐**暂缓**。
7. **实施**：推荐照旧由 Gemini 实施、Claude 核对，结束前清理无关文件；只做离线测试。
8. **记录**：推荐新开 **D-100**。

## 9. 实施记录

日期：2026-11-25。按第 3 节与实施说明完成（全离线验证，未连接本机 Ollama）：

- **设置与回环校验**（`app/config.py`、`.env.example`）：新增 `SUMMARY_MODEL_BASE_URL`（默认 `http://127.0.0.1:11434/v1`）与 `SUMMARY_MODEL`（默认 `qwen3:4b-instruct`），提供 `Settings.summary_model_problem()` 方法校验非空模型名、`http`/`https` 协议及回环主机（`127.0.0.1`、`::1`、`localhost`），不作为字段校验器以免错误 `.env` 导致入口导入失败。
- **依赖与可用性检查**（`app/core/dependencies.py`）：模块级 `conversation_compressor` 与 `open_conversation_compressor()` 均从 `config` 读取地址和模型名；新增 `check_conversation_compressor(compressor, *, timeout_seconds=3.0)`，在超时内调用 `models.list()` 做完整模型名比对，连不上/超时仅暴露 `主机:端口` 与异常类型名（D-033），缺少模型提示 `ollama pull <名字>` 并要求完整模型名。
- **共用打开函数与命令行**（`app/agent/__main__.py`）：新增 `SummaryModelError` 与 `open_history_compressor(enabled, stack)`，返回 `(compressor | None, 'off' | 'on' | 'unavailable')`；检查失败时打印中文提示并降级为不压缩，客户端仍由 `stack` 关闭。
- **网页后端与界面**（`app/api/__main__.py`、`app/api/app.py`、`app/api/static/index.html`、`app/api/static/app.js`）：`python -m app.api` 新增 `--compress-history`，共用 `open_history_compressor`；`GET /api/health` 新增 `history_compression`（`off` / `on` / `unavailable`）；页面顶栏状态区新增“历史压缩”显示（开启 / 关闭 / 不可用，`unavailable` 使用 `warn` 样式，仅用 `textContent`）。
- **限额表**（`app/agent/run_limits.py`）：`--show-limits` 新增“后台摘要”一行，展示本机模型名及从 `SummaryScheduler.__init__` 默认参数读取的超时（30 秒）与最大尝试次数（2 次），不暴露地址。
- **测试**（`tests/test_history_compression_setup.py`、`tests/test_web_api.py`、`tests/test_summary_scheduler.py`）：新增 30 项离线测试覆盖设置默认值、回环校验、可用性检查、共用打开函数四种结果、命令行/网页接入、调度器触发与 `/turns` 原文保持、限额表与静态页面；`tests/test_web_api.py` 逐字比对 `/api/health` 的断言补充 `'history_compression': 'off'`；`tests/test_summary_scheduler.py` 仅给假 `Client` 补 `models.list()` 替身，未改动任何现有断言。
- Claude 核对（2026-11-25）：
  - 逐个看过设置、检查函数、共用打开函数、命令行和网页入口、健康接口、页面和限额表的改动；改了一处：检查不通过时那行提示加上 `flush=True`，网页启动时能马上看到；
  - 自己重跑 `init.ps1`：2382 passed、28 skipped（基线 2352，新增 30 个），`tests/fixtures/` 没有变化，新文件和改过的文件都是 LF；
  - 用户没提交的文件没有被碰过（按校验值核对）；`data/` 下没有新文件，没有遗留的无关文件；
  - 没有连接本机 Ollama，没有调用真实模型。
