# 工具失败和服务商失败的处理（failure-handling-001）：修改方案

日期：2026-11-18。状态：**已实现并提交（2026-11-18，D-085）**。用户 2026-11-18“按推荐值实现”：重试最多 4 次；只读工具抛异常，每次循环最多 3 次。后来用户改为“你来实现”，所以这次由 Claude 实施，没有派 Codex。
实施和方案的两处不同：只跑过只读工具就超时时，如果是时间预算用完了，按 `budget` 结束（和改前一样由预算检查决定），其余按 `timeout`；抛异常的次数按“一次循环”计，阻塞后续跑时重新计数。
来源：用户 2026-11-18 同意先做设计审查（`docs/agent_design_review.md`，暂未入库）里的功能增强，要求一步一步做，每一步先出方案再实现。这是第一步，对应审查报告第 9 节“先做的三件事”的第 1 件：2.1、2.2、2.3，再加上 1.1 里出现在用户提示上的那部分旧词。
依据：当前代码（`973ef0c`，外加本轮修好的 `tests/test_model_profiles.py` 一行，见第 7 节）。

## 1. 现状

| 项 | 现在 | 位置 |
|---|---|---|
| 工具调用抛异常 | 循环直接抛 `StepToolError`，整个循环结束。模型没有机会换一种做法 | `app/agent/agent_loop.py` 第 411～415 行 |
| `LoopRun` 怎么处理 | 写入或运行类工具抛异常时，结果不确定，按 `side_effect` 结束。只读工具抛异常、一般的 `StepRunError`（包括 `ToolRoundLimitError`、`StepBusinessError`），以及只跑过只读工具就超时的，都返回 `_RETRY` | `app/agent/loop_run.py` 第 325～348 行 |
| `_RETRY` 之后 | 第一次执行之后，只有用户回答了新问题才允许再执行一次。可一次 `run()` 里不可能出现新回答，所以 `_RETRY` **一定**以 `limit` 结束，文字是“Agent replanning limit reached; last failure: …” | `loop_run.py` 第 236～238、262～272 行 |
| 用户看到什么 | `limit` 的说明是“已达到执行步数或重新规划次数的上限”。规划模式第 4 阶段就删了，这句话把排查方向带偏。`planning` 这一项已经没有地方用了 | `app/agent/runtime.py` 第 15～16 行 |
| 上下文放不下 | `ContextBudgetError` 继承自 `ModelRequestError`，被包成 `StepModelError`，最后按 `model` 结束，提示“可以稍后重试”。可重试一定还是失败 | `app/services/request_fitter.py` 第 21 行，`runtime.py` 第 18 行 |
| 服务商重试 | SDK 自带的重试关掉了。宿主自己最多重试 2 次，等 0.5 秒、1 秒，总共 1.5 秒。不看 `Retry-After`，也没有抖动。408、409、429 和 5xx 会重试 | `app/agent/openai_compatible_tool_calling_model.py` 第 30、55～56、151～184 行，`rag_agent_service.py` 第 305 行 |
| 实际影响 | 第 5 阶段调参集里 `fix_lru_cache` 失败，就是因为第三次调用连续 5xx，重试两次后放弃。这本是服务商的一次短暂故障，却成了一道题的失败 | 交接 2026-11-15 |
| 黄金文件 | 上面这些行为都被 `tests/fixtures/loop_run_golden/` 固定住了，测试注释写的是“出现差异就是行为变化，要解释，不能直接重录” | `tests/test_loop_run_equivalence.py` 第 1～7 行 |

## 2. 改动一：只读工具出错，把错误交回模型（必做）

- `agent_loop.py`：`self._registry.invoke(call)` 抛异常时，先看这一步的副作用监视（`SideEffectWatch.uncertain`，D-032）：
  - **不确定**（写入或运行类工具抛了异常），或者拿不到监视器：照旧抛 `StepToolError`，任务按 `side_effect` 结束，不重放；
  - **确定没有副作用**：不结束循环。给这次调用回一条工具消息 `{"success": false, "content": "", "error": "tool <名字> raised <异常类型名>; the call did not complete", "source_refs": []}`。只写异常的**类型名**，不写异常文字（D-033），并在 `results` 里记一条失败的 `ToolResult`，这样回执和步骤日志里能看到它。这次调用照常计入工具次数。
- **同一个任务里抛异常的次数有上限**：默认 3 次（第 6 节第 2 问）。第 3 次时抛 `StepToolError`，任务按新的失败类型 `tool` 结束。这是为了 MCP 子进程崩溃这种情况：之后每次调用都会失败，没必要一直试到工具次数用完。
- 这样几种情况都变成模型能看到、能绕开的工具错误，任务不会立刻结束：文件读不到、联网搜索超过 30 秒、`MCPToolAdapter` 遇到非文本结果抛 `TypeError`（`app/tools/mcp_tool_adapter.py` 第 47～48 行）。
- “只读”还是按现在 `SIDE_EFFECT_TOOLS` 的名单判断（`app/agent/side_effects.py` 第 16 行）。审查报告 1.4 要把它改成工具自己声明，放在第 3 步和 `edit_file` 一起做。这一步不扩大名单，也不缩小。
- 团队成员和调研成员用的是同一个 `AgentLoopRunner`，所以自动得到同样的行为。

## 3. 改动二：失败类型重新划分，去掉“重新规划”（必做）

`_RETRY` 一定以 `limit` 结束，所以把它删掉，每种失败直接给出自己的类型。下表是改前和改后。改后的一列就是要重录的黄金文件内容：

| 情况 | 现在 | 改后 | 说明文字（`FAILURE_MESSAGES`） |
|---|---|---|---|
| 工具轮数或次数用完（`ToolRoundLimitError`） | `limit`，“replanning limit reached; last failure: …” | `limit`，直接用它自己的说明 | 改为：“已达到工具调用次数或执行次数的上限，任务停止。” |
| 第二次执行被拒（`max_executions`） | `limit` / `internal` | 不变 | 同上 |
| 只读工具抛异常达到上限，或模型反复请求不存在的工具（一般的 `StepRunError`） | `limit` | **`tool`**（新增） | “工具调用反复出错或不可用，任务停止。可以检查工具服务（文件系统、联网搜索、沙箱）是否正常。” |
| 只跑过只读工具就超时 | `limit` | **`timeout`** | 原来的 `timeout` 说明是“为避免重复已完成的操作……”，改成通用的：“执行超时，任务停止。已完成的修改不会重复执行。” |
| 模型明确报告做不成（`StepBusinessError`，结果为 `FAILED`） | `limit` | **`business`** | 不变：“任务无法完成。” |
| 上下文放不下（`ContextBudgetError`，或者溢出重试之后服务商仍然报“上下文太长”） | `model` | **`context`**（新增） | “这次任务需要的上下文超过了模型窗口，重试也放不下，任务停止。可以缩小任务范围，或者换一个窗口更大的模型档案。” |
| 写入或运行类工具抛异常、写入后超时、团队超时 | `side_effect` / `timeout` | 类型不变 | 只改英文的宿主文字：去掉“automatic replanning is disabled”，改成“it will not be repeated” |

- `planning` 从 `FAILURE_MESSAGES` 里删掉，生产代码里已经没有地方用它。`TaskStatus.PLANNING` 是任务状态，不是失败类型，不动。
- `context` 的判断方式：沿着 `StepModelError` 的异常链找 `ContextBudgetError` 或 `ModelContextOverflowError`，和 `_policy_denial` 一样，放在 `runtime.py`。
- 另外三处英文的宿主文字也一起改掉：`_check_resource_context` 里的“install a fresh plan”，`_last_failure` 只在 `tool` 失败时附上，以及日志里的“replanning”。
- **这一步不动的**：`replan_count` 字段（恒为 0）、`plan` 事件、`PlanStep`。网页的时间线、轨迹和评测解析都读它们，要改得整体做一次词汇迁移（审查报告 1.1），这次不做，免得改动面太大。
- `docs/agent_runtime.md` 的结果对照表（第 52～66 行）按上表更新。

## 4. 改动三：服务商重试按时间预算，加抖动，遵守 `Retry-After`（必做，参数见第 6 节第 1 问）

在 `openai_compatible_tool_calling_model.py` 里改：

- 默认最多重试 **4** 次，基础等待 1、2、4、8 秒，每次乘一个 0.5～1.0 之间的随机系数（抖动，避免很多请求同时重试），每次最多等 30 秒。随机数函数可以注入，测试用固定值。
- 服务商返回了 `Retry-After`（秒数或 HTTP 日期）或 `retry-after-ms` 时，按它的值等，最多 60 秒。超过 60 秒就不等了，直接失败，类型仍是 `model`。只读这个响应头，不读、不记录响应正文（D-039）。
- 预算约束不变，而且照现在的做法检查：令牌预算已经用完，或者任务剩下的时间不够等这一次，就不再重试（`RunMeter.budget_problem()`、`remaining_seconds()`）。任务的总时限仍然由 `LoopRun` 的唯一外层时限保证。
- 哪些错误可以重试不变：408、409、429、5xx、连接错误、超时、无效响应、空回复。
- 日志只记异常类型、第几次、等多久，不记服务商文字（现在就是这样）。
- `python -m app.agent --show-limits` 加一行“模型调用重试：最多 4 次，等 1/2/4/8 秒（带抖动，每次最多 30 秒），遵守 Retry-After（最多 60 秒）”，所有限额继续收在一处显示（D-081）。
- 这一步**不做**备用模型档案（主档案连续 5xx 时换一个）。需要的话单独做，它要配合 `models.toml`。

## 5. 测试和记录

- **黄金文件**：只允许下面这些文件变化，每个变化都要对上第 3 节的表。实施时在 PR 说明里逐个列出“改前 → 改后”，其他黄金文件一个字节都不能变：
  - `test_host_boundaries_`：`business`、`failed-output`、`run-error`、`timeout-read`、`tool-limit`，以及只改英文文字的 `timeout-write`、`timeout-team`；
  - `test_model_scenarios_`：`read-error`、`tool-round-limit`、`unavailable-tool`、`run-error`、`write-error`（后两个只改文字）；
  - `test_real_team_timeout`（只改文字）。
  - `read-error` 场景的脚本加一轮 `ModelTurn('answer')`，改后应该是“工具错误交回模型，模型照常回答，任务完成”。
- **新增测试**：
  - 只读工具抛异常后，模型收到的工具消息里有类型名，没有异常文字，下一轮照常继续，任务完成；
  - 同一任务里第 3 次抛异常时，任务按 `tool` 结束；
  - 写入或运行类工具抛异常时，仍然按 `side_effect` 结束，而且不会再执行一次；
  - 只读超时按 `timeout` 结束；
  - `ContextBudgetError`，以及溢出重试后仍溢出，都按 `context` 结束；
  - MCP 非文本结果造成的 `TypeError` 走只读工具出错的路径；
  - 团队成员里只读工具抛异常，成员能继续。
- **重试测试**（`tests/agent/test_model_retry.py`）：
  - 现有的等待时间断言改成新的序列（用固定的随机系数）；
  - 新增：`Retry-After` 秒数和 HTTP 日期两种写法、`retry-after-ms`、超过 60 秒就放弃、剩余时间不够时放弃、抖动系数的范围、`--show-limits` 里有这一行。
- **记录**：
  - `docs/DECISIONS.md` 新增 D-085：失败类型新增 `tool`、`context`，删去 `planning`，`limit` 只表示次数上限，只读工具出错交回模型；
  - D-039 的条目补一句：重试参数改了，指向 D-085；
  - `docs/agent_runtime.md`、`docs/PROJECT_HANDOVER.md` 的相关段落；
  - 交接文件只在 `handoff` 分支上更新（D-077）。
- **文档模块**：`docs/modules/` 里摘录了这些代码（02、03、07、10），实现后由 Claude 同步更新摘录，并用摘录核对脚本再查一遍。

## 6. 要你定的

1. **重试的力度**：
   - 推荐：最多 4 次，大约 7～15 秒，遵守 `Retry-After`，最多等 60 秒；
   - 更积极：最多 6 次，大约 30～60 秒，代价是服务商真的挂了时，一道题要多等将近 1 分钟才失败；
   - 保守：维持 2 次，只加抖动和 `Retry-After`。
2. **只读工具抛异常的上限**：
   - 推荐：每个任务 3 次；
   - 不设上限，只受工具次数（40 次）约束。

## 7. 约束和本轮已做的小改动

- 不改 `.env`、模型默认值和依赖，不调用真实模型，不跑评测。实现完由 Claude 审查，看范围、黄金文件的差异、`init.ps1` 和用户未提交的改动。提交和推送要用户同意。
- 用户自己删掉的 `scripts/benchmark_conversation_memory.py`，以及还没提交的 `docs/modules/`、`docs/DOCUMENT_INDEX.md`、`docs/agent_design_review.md`，Codex 都不碰。
- 本轮 Claude 已直接修好一行测试：`tests/test_model_profiles.py` 第 423 行补上 `--confirm-system-drive`。原因是 Windows 上 `tmp_path` 在系统盘，命令行先被工作目录检查拦下（D-074），轮不到模型档案检查。修改后 `tests/test_model_profiles.py` 34 项全部通过。
- 真实效果要等用户授权跑一次调参集才能看到。预期是 5xx 那类失败会少，但服务商状态的干扰（审查报告 4.3）这一步不处理。
