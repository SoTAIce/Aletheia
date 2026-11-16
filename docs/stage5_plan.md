# 第 5 阶段：限额、时限和上下文整理（方案）

日期：2026-11-16。状态：**已实现（第 8 节，D-081、D-082）**（stage5-limits-context-001）。
来源：用户 2026-11-16 选择“先写第 5 阶段方案”。原来的迁移路线（`docs/architecture_review.md` 第 6 节）只有第 0～4 阶段，已全部完成；“第 5 阶段”指架构梳理里还没解决的问题 3（限额和时限分散）和问题 4（上下文策略和模型包装拼凑）。
依据：当前代码 `84b530e`（钩子加固 D-080 之后）逐个文件清点，不来自历史报告。

## 1. 现在的状况

### 1.1 时限：名义上 7 层，实际起作用的和写着的不一致

| 时限 | 在哪里 | 默认值 | 实际情况 |
|---|---|---|---|
| 单次模型请求 | `rag_model_timeout_seconds` → `ModelProfile.timeout_seconds` | 30 秒（用户 `.env` 是 90） | 生效 |
| 选模式的那次调用 | `route_selection.DEFAULT_TIMEOUT_SECONDS` | 20 秒 | 生效 |
| 单一循环 | `LoopOptions.timeout_seconds` | 480 秒 | **循环里没有执行**。它只被 `RunAssembler` 用来把“单步时限”放宽到 485 秒 |
| 单步 | `agent_step_timeout_seconds`，`LoopRun` 里 `asyncio.timeout` | 60 秒（用户 `.env` 是 240） | 单一循环和团队下总被放宽成 `max(设置值, 循环或团队时限 + 5)`，即 485 / 185 秒，**用户设的 240 在这两种模式下都不起作用**；带沙箱时命令行还要再按沙箱时限放宽一次（`step_timeout_for`） |
| 团队 | `DelegationOptions.timeout_seconds`，负责人循环外的 `asyncio.timeout` | 180 秒 | 生效；外面还套着 185 秒的单步时限 |
| 团队成员 | 成员的 `LoopOptions(…, o.timeout_seconds)` | 180 秒 | **没有执行**（同第 3 行，循环不读这个值），成员只受团队总时限约束 |
| 调研成员（`--delegate`） | `ExploreOptions.member_timeout_seconds` | 120 秒 | 生效 |
| 整个任务 | `RunMeter` 的时间预算（`--max-seconds`） | 不限 | 生效，`meter.bound()` 会截短单步时限 |
| 门控 | `hooks.GATE_TIMEOUT_SECONDS` | 5 秒 | 生效 |

等待人工确认不计时，靠 `pausable_deadline`，在 `loop_run.py` 和 `delegation.py` 各套一次。

### 1.2 次数上限：有一组完全不生效

- `LoopRun(max_tool_rounds=4, max_total_tool_calls=8)` → `RunnerLimits` → `RunnerDependencies.limits`：**没有任何执行器读取**。这就是问题 3 里“每步 4 轮 8 次”的残留；现在单一循环用的是 `LoopOptions` 的 20 轮 40 次。
- `LoopRun` 还保留着规划—执行的外壳：`needs_plan`、`PlanStep`、`plan_revision`、`replan_count`，以及固定为 0 的 `_max_replans`。结果是：一步因为只读工具出错或只读超时而失败时，代码先“准备重新规划”，下一圈立刻以“Agent replanning limit reached”（类型 `limit`）结束。行为没错，但读代码的人会以为还有重新规划。
- `max_executions=16`：没有重新规划之后，第二次执行只发生在“阻塞→用户回答→续跑”时，所以它实际上是“一个任务最多续跑 15 次”。
- 其余上限各管一件事，没有重复：团队 `max_delegations` 6、`max_lead_turns` 16（同时当轮数和调用数用）、成员 12 轮 24 次、报告 1500 字符；调研成员 2 次、16 次调用、4/8/12 轮、`max_round_share` 0.75、任务描述 4000 字符；`todo` 20 项、问题 1000 字符。

### 1.3 令牌：各管各的，问题不大

`RunMeter` 的 `--max-tokens`（一个任务）、`conversation_max_tokens`（一个会话）、模型窗口、输出、安全余量（来自模型档案，D-078）、工具结果内联上限（按窗口推出，D-052）、长期记忆 `longterm_context_tokens`、知识库 `rag_context_token_budget`。每个只在一处执行；只是没有一处能一眼看全。

### 1.4 一次模型调用经过的层（问题 4）

现在：`HookedModel`（服务层装）→ `AgentLoopRunner._generate`（每次调用前查预算，4d-3）→ `CompactingManager.generate`（折叠旧的文件内容）→ `ContextManager.generate`（溢出后缩小重试）→ 适配器。

和梳理时相比已经好了很多：`ContextManagedModel`、`PlanningModel`、团队的 `_Counted` 和 `_CompactingManager` 都已删除；旧工具结果折叠已经对所有循环生效，不再只给团队成员。还剩的问题：

- `ContextManager`（367 行）仍然一个类管四件事：对话历史准备（`prepare_conversation`）、长期记忆片段（`longterm_context`）、工具结果截断和转存（`project_tool_result`）、请求压缩和溢出重试（`prepare` / `generate`）。
- `CompactingManager` 用 `__getattr__` 把所有属性转给里面的 `ContextManager`，从调用处看不出调用的是哪一个。
- “请求能不能放进窗口”在三处判断：`CompactingManager`（60% / 85%）、`ContextManager.prepare`（缩小工具结果）、溢出后重试。

## 2. 目标

1. 每个限额只在一层执行，写着的值就是起作用的值；不起作用的参数删除。
2. 有一处地方能看全所有限额（一个数据类和一张表，命令行能打印出来）。
3. 时限分层清楚：任务、单次模型请求、成员、门控，等待确认的时间统一不计。
4. 上下文的四件事拆开，一次请求的“放进窗口”只在一处决定。
5. 默认设置下发出的请求逐字节不变（沿用 `tests/test_model_profile.py` 的对照方法）；改变行为的地方逐条列在第 3 节并由你确认。

## 3. 设计（分三步，每步单独提交、单独可回退）

### 5a：删除不起作用的东西（行为不变）

- 删除 `RunnerLimits`、`RunnerDependencies.limits`，以及 `LoopRun` 的 `max_tool_rounds` / `max_total_tool_calls` 参数。
- 去掉 `LoopRun` 里的规划外壳：每次执行就是“跑一次执行器”；只读失败或只读超时直接结束，失败类型和给用户的说明不变（仍是 `limit`；文字改成说清原因，例如“只读工具出错，任务结束；不会自动重试”）。`plan` 事件照旧发出（网页时间线在用，`app.js` 第 869、1052 行）。
- `replan_count` 怎么办见第 7 节第 3 问。
- `LoopOptions.timeout_seconds` 暂时保留，5b 处理。

### 5b：时限分层（会改变行为，见第 7 节第 1、2 问）

建议的分层：

| 层 | 管什么 | 执行处 |
|---|---|---|
| 任务时限 | 一次执行从开始到结束（续跑重新计时） | `LoopRun`，唯一的外层 `asyncio.timeout`，确认等待不计 |
| 成员时限 | 团队成员、调研成员各自的上限，且不超过任务剩余时间 | 委派处，复用同一个可暂停的截止时间 |
| 单次模型请求 | 一个请求 | 模型档案，不变 |
| 门控、选模式 | 各自一次调用 | 不变 |
| 任务预算 | `--max-seconds` / `--max-tokens` | `RunMeter`，不变 |

- `LoopOptions.timeout_seconds` 和 `agent_step_timeout_seconds` 合成一个“任务时限”；单一循环默认 480 秒，团队默认 180 秒（都是现在实际起作用的值，减掉那 5 秒余量）。
- 删掉 `RunAssembler` 和命令行里的两处“放宽单步时限”：任务时限只有一个值，沙箱的单次调用时限要小于它，启动时检查，不满足就报错说明，而不是悄悄放宽。
- 团队成员真正有自己的时限（默认 120 秒，和调研成员一致），这是**新加的限制**；成员超时按现有的成员失败处理，负责人可以再委派。
- 新增 `app/agent/run_limits.py`：一个冻结数据类收齐第 1.1、1.2 节的值，由设置和命令行参数构造，`LoopRun`、执行器、委派都从它读取；`python -m app.agent --show-limits` 打印一张表（不调用模型）。

### 5c：拆分上下文管理器（行为不变）

- `ContextManager` 拆成三个类，各自一个文件：
  - `ConversationContext`：对话历史和长期记忆片段（现在的 `prepare_conversation`、`longterm_context`）。
  - `ToolResultProjector`：工具结果截断和转存（`project_tool_result`）。
  - `RequestFitter`：一次请求放进窗口——先做现在 `CompactingManager` 的两级折叠，再做 `prepare` 的缩小，溢出后重试。合并后 `CompactingManager` 和它的 `__getattr__` 删除。
- 这一步先保留 `ContextManager` 作为把三者组起来的门面，调用方不改；全部迁移后再决定是否删掉门面。
- 验证：拿现有测试里的对话和工具结果，新旧两条路径发出的请求逐字节相同。

## 4. 要改的文件（预计）

- 5a：`app/agent/loop_run.py`、`app/agent/step_runner.py`、`app/services/run_assembler.py`、`app/agent/runtime.py`（看第 3 问）、相关测试。
- 5b：新增 `app/agent/run_limits.py`；`app/config.py`、`app/agent/agent_loop.py`、`app/agent/delegation.py`、`app/agent/__main__.py`、`app/api/__main__.py`、`app/services/rag_agent_service.py`、`app/services/run_assembler.py`、`bench/run_bench.py`（时限参数）、`.env.example`、`bench/README.md`。
- 5c：`app/services/context_manager.py` 拆成三个新文件，`app/agent/model_view.py`、`app/agent/agent_loop.py`。
- 记录：`docs/DECISIONS.md`（D-081 起）、`docs/DOCUMENT_INDEX.md`、`CLAUDE.md` 架构概览、`docs/agent_runtime.md`。

## 5. 测试和评测

- 5a：删掉测 `RunnerLimits` 的测试；只读失败、只读超时、写入后超时三种结束方式各有一项测试，失败类型和文字固定。
- 5b：每个限额一项“写着的值就是起作用的值”的测试（用假模型和假时钟，不真正等待）；确认等待不计入任务时限和成员时限；沙箱时限大于任务时限时启动报错；`--show-limits` 的输出。
- 5c：请求逐字节对照；三个类各自的单元测试；现有上下文测试不改就能通过（门面还在）。
- 全部做完后，可选在调参集上跑一次单一循环和团队（真实模型调用，要你授权），因为 5b 改了时限语义。

## 6. 不做的

- 不改各项上限的默认数值（第 5b 节新增的成员时限除外）。
- 不改按 4 字符一令牌的估算，不引入分词器。
- 不做 `models.toml` 和多模型角色（4d-2、4d-4，等有第二个模型再做）。
- 不改 `.env` 和模型默认值；`.env` 里已经不用的 `AGENT_PLANNING_TIMEOUT_SECONDS` 由你自己决定删不删（设置会忽略它）。

## 7. 需要你确认的问题

1. **5b 的时限语义**：(a) 按第 5b 节分层，一个任务时限，成员各有时限且不超过剩余时间（建议）；(b) 只做 5a 和 5c，时限保持现在的多层。
2. **`AGENT_STEP_TIMEOUT_SECONDS` 怎么处理**：现在它在单一循环和团队下都不起作用（总被放宽到 485 / 185 秒）。(a) 新设一个任务时限设置，旧名字不再读取，并在 `.env.example` 和文档里说明（建议；你本机 `.env` 里的 240 不会悄悄变成 240 秒的任务上限）；(b) 旧名字继续读取并当作任务时限——那么你的单一循环任务会从约 485 秒变成 240 秒。
3. **`replan_count`（以及命令行状态行里的 `replans:`）**：(a) 保留字段，恒为 0，评测解析和旧结果文件不用改（建议）；(b) 删除，同步改评测解析和结果文件格式。
4. **由谁实现**：Codex 额度 11 月 15 日已恢复。(a) 按分工由 Codex 实现、Claude 审查（建议）；(b) Claude 直接实现。
5. **做完后要不要跑一次调参集**（单一循环和团队，真实模型调用）：(a) 做完 5b 后跑一次（建议，到时再单独授权）；(b) 不跑。

**用户回答（2026-11-16）**：第 1 问 (a) 按 5b 分层；第 2 问 (a) 新设任务时限设置，旧名字不再读取；第 3 问 (a) 保留 `replan_count`，恒为 0；第 4 问由 Claude 直接实现；第 5 问没有单独回答，做完后再单独请求授权。

## 8. 实现记录（2026-11-16，Claude）

三步各自一个提交，都在本地 `main` 上：`a477484`（5a）、`4c8d6f0`（5b）、`9e55f87`（5c）。全部测试（云端 Linux）：1862 通过、22 跳过、4 失败（只在 Windows 上成立的路径断言，同前）。`init.ps1` 没在 Windows 上跑。

**5a（行为不变）**
- 删除 `RunnerLimits`、`RunnerDependencies.limits`，以及 `LoopRun` 的 `max_tool_rounds` / `max_total_tool_calls` 参数；再传这两个参数会报 `TypeError`。
- `LoopRun._run` 改成直线流程：先查（`_pending_stop`：阻塞问题和预算），再判断能不能开始（`_start_problem`：执行次数，以及续跑必须有新回答），然后发一次 `plan` 区间和事件，执行一次，最后完成。
- 和第 3 节原计划不同：**失败文字没有改**。67 个黄金对照文件（`tests/fixtures/loop_run_golden/`）固定了每种结束方式的结果、事件和文字，包括“Agent replanning limit reached”和两处执行次数上限分别是 `internal` 和 `limit`。这些文件写明“差别是要解释的行为变化”，所以 5a 保持它们全部通过，只改内部结构。
- 删掉了实际到不了的“Planning failed”分支；`FAILURE_MESSAGES['planning']` 保留，给旧结果文件用。

**5b（行为改变，已按第 7 节确认）**
- 任务时限就是执行器自己的时限，由 `LoopRun` 执行一次：单一循环 480 秒（新设置 `AGENT_TASK_TIMEOUT_SECONDS`），团队 180 秒（`--team-timeout`）。`RunAssembler` 和命令行不再放宽（原来是“设置值和执行器时限 + 5 取大”）。`AGENT_STEP_TIMEOUT_SECONDS` 不再读取；你本机 `.env` 里的 240 现在不起作用，可以删掉。
- 团队负责人循环里那层 `asyncio.timeout` 删除，团队时限就是 `LoopRun` 的任务时限；超时后的结果和文字不变。
- 团队成员新加 `DelegationOptions.member_timeout_seconds`（默认 120 秒），嵌套在任务时限里，等待确认时暂停。成员超时写成成员失败报告（`failure: TimeoutError`，附说明），负责人可以继续；任务时限先到时仍是整个团队 `timeout`。调研成员的 120 秒时限同样改为可暂停。
- 带沙箱时，任务时限必须长于一次沙箱调用（默认 90 秒），否则启动时报 `DeadlineError` 并退出，不再悄悄放宽。默认值 480 / 180 都满足。
- `app/agent/run_limits.py`：和第 3 节原计划不同，`RunLimits` **只把各层的值收在一起显示和检查**，各层仍读自己的选项（`LoopOptions`、`DelegationOptions`、`ExploreOptions`、模型档案）。这样每个限额仍只在一处执行，又不用把同一个值同时放在两个地方。`python -m app.agent --show-limits` 打印全部限额，不调用模型。
- 黄金对照的构造函数原来照搬旧装配规则（时限至少 60 秒）；改成新规则后，团队超时那一项从等满 60 秒变回 0.02 秒，67 项仍全部通过。

**5c（行为不变）**
- 新文件：`context_budget.py`（共享的窗口、输出、余量和计数器）、`conversation_context.py`、`tool_result_projector.py`、`request_fitter.py`。`ContextManager` 是门面，原有方法名、属性（可读可写）、类常量和错误文字不变；`LONGTERM_POLICY` 和 `ContextBudgetError` 仍可以从原模块导入。
- `CompactingManager` 删除。它的两级折叠成了 `RequestFitter.compact`，循环调用 `generate(..., compact=True)`；其他直接调用 `generate` 的地方（知识库工具、查询改写）和以前一样不折叠。原来记的 `compacted` / `strong` 两个计数没有任何地方读取，没有保留。
- 对照：拆分前先用旧代码录了 `tests/fixtures/context_split_golden.json`（一个循环 15 次请求，覆盖两级折叠、参数省略、旧输出缩短；一次溢出重试；`prepare` 的五种窗口；六个工具结果的视图和转存；请求计数），拆分后逐字节相同。反向检查：把第二级阈值从 0.85 改成 0.9、或把预览下限从 1200 改成 1201，这项测试都会失败。

**没做的**
- 第 5 问的调参集评测：5b 改变了团队成员的时限和单一循环的时限（485 → 480 秒），要不要跑一次，等你单独决定（真实模型调用）。
