# 第 4 阶段：拆服务层、循环独立运行、删除规划—执行骨架（修改方案）

日期：2026-11-11。状态：**用户已确认第 7 节；4a 已提交 `e6a9856`；4b 已提交 `70f7470`；4c 已实现并评测，未提交（第 14 节）**（stage4-plan-001；4a 为 stage4a-001）。
来源：用户 2026-11-11“提交吧，之后开启第四阶段”。依据是当前代码（`a0aa489`）、[架构梳理](architecture_review.md) 第 3、5、6 节和第 3 阶段的结果（[stage3_team_delegate_plan.md](stage3_team_delegate_plan.md) 第 10 节）。

## 1. 第 4 阶段原定内容和现在的情况

架构梳理第 6 节的原文：“精简任务状态，拆分服务层，加模型档案；删除规划—执行骨架。判断标准：测试和两套评测不回退。”第 1～3 阶段之后，情况如下：

| 原定项 | 现状 |
|---|---|
| 规划—执行骨架 | 单一循环和团队都已经是一个循环，但两者仍然通过“固定一步计划”进入 `AgentRuntime` → `Executor` → `StepExecutor`，只是借用它的取消、超时、重新绑定、版本校验、预算和结果提交。真正用到“多步计划”的只剩规划模式（`--single-runner plan`）：命令行、网页、评测默认都不用它（D-072），但服务层的库默认值仍是 `plan`，网页的执行方式下拉框里也能选它 |
| 任务状态 | `TaskState` 有 928 行、35 个方法。循环路径实际只读 `task_id`、`active_goal`、`goal_revision`、`constraints`、`pinned_contexts`、`resolved_questions`；计划修订号、步骤状态机、草稿区（中间结果、最近观察、失败记录）只为规划模式服务 |
| 服务层 | `RagAgentService.query_result` 有 276 行（梳理时约 220 行），按需委派又加了一段执行器工厂 |
| 模型档案 | 只有一个模型（DashScope 兼容接口），价格写死在 `run_meter.estimated_cost` 里 |
| 测试 | 58 个测试文件里，引用 `taskstate` 的有 43 个，引用 `working_memory` 的 31 个，引用 `planner`、`step_executor` 的各 14 个，引用 `runtime` 的 13 个 |

还有两个约束：

- **你的未提交改动正好在骨架里**：`planner.py`、`step_executor.py`、`tool_calling_step_runner.py` 的改动是把文档字符串改写成中文说明，共 119 行新增。删除骨架就会删掉这三个文件，见第 7 节第 1 问。
- **`AGENTS.md` 的第一条全局不变量**是“保留 Plan–Execute–Replan：Planner 提议、Executor 安装、Runtime 调度、StepExecutor 校验并提交”。删除骨架要先改写这一条，见第 7 节第 2 问。

## 2. 目标

```
入口（命令行 / 网页）
   │
会话服务  SessionService：会话、归档、幂等、长期记忆召回、结果发布
   │
运行装配  RunAssembler：按请求选项组装 模型 + 工具集 + hooks + 执行方式 + 预算
   │
任务运行  LoopRun：一个任务一个循环；负责取消、超时、重新绑定、预算、阻塞和续跑、结果
   │
循环      AgentLoopRunner（单一循环；团队 = 负责人循环 + delegate；按需委派）
状态      RunState：目标、状态、待办、问题与回答、用量、结果
```

宿主保证（`AGENTS.md` 的取消、超时、重新绑定、版本校验、预算上限，格式修复不重放工具，副作用不确定不自动重试）全部保留，只是从“步骤”层挪到“任务运行”层。第 3 阶段的 `_BoundRunner` 已经在运行时里实现了重新绑定作废和取消后不提交，这次整体搬进 `LoopRun`。

## 3. 分步（每步单独评测、单独提交，只有前一步不回退才做下一步）

### 4a 拆服务层（不碰你的文件，不改行为）

- 把 `query_result` 拆成三块：
  - `SessionService`：请求校验、幂等和重放、会话加载、长期记忆召回、结果发布、异常收尾；
  - `RunAssembler`：执行方式选择（含 `route_selection`）、只读收回、按执行方式选执行器工厂、建或重新绑定运行时；
  - 原来的 `RagAgentService` 作为门面保留，构造参数、公开方法、事件流都不变，命令行和网页不用改。
- 验收：
  - 全部现有测试不改就能通过（只允许改直接引用私有属性的测试）；
  - `query_result` 降到 80 行以内；
  - 不需要真实模型评测，行为不变由测试保证；若你要求，可以跑一次调参集确认。

### 4b 循环脱离骨架（不碰你的文件）

- 新增 `app/agent/loop_run.py`：
  - `RunState` 只保存循环要用的字段：任务编号、目标和目标修订号、状态、约束、固定上下文、已回答的问题、待办、用量、结果；
  - `LoopRun` 负责执行：单一循环、团队、按需委派都直接在 `LoopRun` 里跑，不再经过固定一步计划、`Executor` 和 `StepExecutor`。
- 结果对象（`AgentRunResult`）的字段保持不变。阻塞后续跑沿用 `LoopResumeStore`。
- 事件流兼容：网页的执行树（`app.js`）按 `plan`、`step` 事件画图。循环路径继续发一个只有一步的 `plan` 事件和 `step` 事件，字段不变，前端不用改。
- 规划模式这一步不动，仍走旧骨架；所以 `TaskState`、`WorkingMemory` 和骨架在 4b 期间都还在，只是循环路径不用它们。
- 详细设计见第 9 节（2026-11-11 补写）。上面三条的细节以第 9 节为准。
- 验收：
  - 调参集单一循环、团队各跑 1 次，不低于 3b（单一循环 10/11，团队 11/11）；
  - 循环路径的令牌和调用次数不升；
  - 新旧两条路径的逐项对照测试全部一致（第 9.5 节）。现有测试不改就能通过；宿主保证测试的迁移留到 4c。

### 4c 删除规划模式和骨架（要你先决定第 7 节第 1、2 问）

- 删除：
  - `--single-runner plan`，以及网页的执行方式选项（只剩单一循环；下拉框移除，`runners` 字段保留但只有 `loop`，兼容旧前端）；
  - `planner.py`、`executor.py`、`step_executor.py`、`tool_calling_step_runner.py`、`runtime.py` 里规划和重新规划的部分；
  - `StepRoutingError`；
  - `TaskState` 里计划和步骤的状态机、草稿区；
  - 只为规划模式写的提示规则（D-037、D-052、D-053 的相关部分）。
- 服务层的库默认值改为 `loop`。
- `TaskState` 和 `WorkingMemory`：
  - 会话和长期记忆还在用其中的固定上下文和约束，所以不整个删除，只缩到 `RunState` 需要的部分；
  - 还有调用方的旧名字，用薄的兼容层保留一版。
- 测试：
  - 只测规划模式内部的测试删除；
  - 测宿主保证的测试迁移到 `LoopRun`；
  - 删除和迁移的清单在实现时列进本文件，先给你看再删。
- `AGENTS.md`：改写第一条全局不变量（第 7 节第 2 问），并在 `docs/DECISIONS.md` 新增一条决策。
- 验收：
  - 调参集单一循环、团队不低于 4b；
  - 留出集单一循环跑 1 次，不低于 10/13。D-072 记录的单一循环三次留出集是 12/13、10/13、11/13，规划模式是 11/13。这是第 4 阶段唯一一次跑留出集，按 D-035 只看结果，不据此调提示。

### 4d 模型档案（可选，建议推迟）

- 把模型地址、上下文窗口、价格、支持的参数、是否能用提示缓存收进一个档案结构，`estimated_cost` 从档案读价格；只有一个档案时行为不变。
- 建议推迟：现在只有一个模型，档案带不来行为上的好处。它真正的用处是让调研成员换便宜模型，从而让按需委派重新值得评测（[loop_delegate_plan.md](loop_delegate_plan.md) 第 11 节）。等你决定要第二个模型时再做。

## 4. 不改的

- hooks、副作用保护、宿主权限、只读收回、对话归档和幂等、工具注册表和 MCP、`RunMeter`、失败分类和中文说明、缓存友好的请求开头（架构梳理第 4 节）。
- `route_selection` 的 `auto` 路由（单一循环和团队之间选）。
- 团队的提示、预算和完成门槛（D-075）；按需委派保持默认关闭。
- `.env`、模型默认值、依赖。

## 5. 风险

- **4b 的事件流**：网页执行树、追踪查看器（`app.agent.trace`）、评测的 `steps` 字段都读 `plan` 和 `step` 事件。保持“一步计划”的事件形状可以兼容，但要逐个核对。
- **4c 删测试多**：涉及 40 多个测试文件；D-072 还记录了 60 多项服务测试依赖库默认值 `plan`，要逐个判断是改成循环还是删除。按“测宿主保证的迁移、只测规划内部的删除”的原则，先列清单再动手。
- **规划模式的用户**：网页里选过“按计划”的用户会失去这个选项。D-072 以来各入口默认都是循环，调参集里循环不低于规划模式。
- **工作量**：4a、4b 各约一个第 3 阶段子步的规模，4c 更大。若 Codex 有额度，按约定交给它实现，Claude 写任务说明并审查；额度不够时由 Claude 来做。

## 6. 评测汇总

| 步 | 需要的真实模型运行 |
|---|---|
| 4a | 无（可选：调参集单一循环 11 次） |
| 4b | 调参集单一循环 11 次 + 团队 11 次（`--write --sandbox`，同 3b） |
| 4c | 调参集单一循环 11 次 + 团队 11 次；留出集单一循环 13 次 |

每一步都要你在当轮授权后才跑。

## 7. 需要你确认的问题

1. **规划模式和你的三份笔记**：删除骨架（4c）会删除 `planner.py`、`step_executor.py`、`tool_calling_step_runner.py`，你在这三个文件里写的中文说明会随之消失。可选：
   - (a) 你先把笔记保存到别处（例如自己提交到一个分支），然后按方案删除；
   - (b) 保留规划模式作为旧路径，第 4 阶段只做 4a、4b，骨架留着，`TaskState` 精简不了多少；
   - (c) 4c 先不做，以后再说。

   建议 (a)：规划模式已经不是默认，评测上也没有优势，留着它会让 `TaskState` 和 40 多个测试文件一直背着计划状态机。
2. **改写全局不变量**：`AGENTS.md` 第一条改为类似“一个任务一个循环：LoopRun 负责取消、超时、重新绑定、预算和结果提交；循环只返回输出，不直接改权威状态”，并新增决策记录。可以吗？（不改就不能做 4c。）
3. **顺序**：4a → 4b → 4c，4d 推迟，可以吗？
4. **删除测试**：4c 按第 3 节的原则删改测试，清单先给你看再删，可以吗？
5. **由谁实现**：按约定 Codex 有额度就交给它，Claude 审查；否则 Claude 来做。上次 Codex 卡住 71 分钟，这次给它的任务会拆得更小，并且只给 4a。

**用户回答（2026-11-11）**：“都按建议”。即：
- 第 1 问选 (a)：做 4c 之前，你自己先保存三个文件里的笔记，然后按方案删除；
- 可以改写 `AGENTS.md` 第一条不变量；
- 顺序按 4a → 4b → 4c，4d 推迟；
- 4c 删改测试前，先把清单给你看；
- Codex 有额度就由它实现，先只给 4a。

## 8. 实现记录

### 4a 拆服务层（2026-11-11，stage4a-001）

Codex 做完 `RunAssembler` 后又卡住了 20 多分钟（同样停在 `node_repl` 调用上），任务记录也消失了。剩下的部分由 Claude 完成。用户的四个未提交文件开工前后 sha256 一致。

- 新增 `app/services/run_assembler.py`，类 `RunAssembler`（Codex）。它接手执行方式的选择和记录、只读收回、`route` 事件、规划器和执行器工厂的选择、限额调整、建立或重新绑定运行时，以及运行本身。`routes` 和 `task_options` 两个字典仍归服务所有，传引用给它。`_choose_route` 和 `_without_writes` 也随之搬过去。
- `RagAgentService.query_result` 从 276 行降到 50 行（Claude）。其余部分拆成 8 个私有方法：
  - `_check_request`：请求参数校验；
  - `_session_conversation`：会话加载；
  - `_replay`：幂等和重放检查；
  - `_open_task`：打开续跑任务或新任务；
  - `_publish_input`：先持久化输入；
  - `_prepare_task`：上下文、附件、工作目录提示、对话历史、长期记忆召回；
  - `_execute`：打开模型和工具并运行；
  - `_settle_stale`、`_settle_failure`：异常收尾。
- 等价性用脚本逐行核对：新旧两版里，原有语句一条不少，只多了方法签名和调用这类衔接代码。唯一改写的一处是 `if existing is not None:`，改成了提前返回。锁的范围、先持久化输入再做外部调用的顺序、异常类型和消息文字都不变。
- 现有测试一项没改，全部通过。新增 `tests/test_run_assembler.py`（6 项），其中一项限制 `query_result` 不超过 80 行。`init.ps1` 结果：1833 项通过、16 项跳过。
- 没有跑真实模型评测：行为不变由测试保证，方案第 6 节写明 4a 不需要评测。


## 9. 4b 详细设计（2026-11-11）

依据：`runtime.py`、`step_executor.py`、`working_memory.py`、`taskstate.py`、`rag_agent_service.py`、`run_assembler.py`、`tool_loader.py`、`knowledge_registry_tool.py`、`app/api/runs.py`、`app/api/static/app.js`、`bench/run_bench.py`，均为当前代码（`e6a9856`）。

### 9.1 现在循环路径实际经过什么

单一循环和团队走同一条路径：

`AgentRuntime.run` → `_run`。具体经过：
1. `TaskState.start_planning` 进入规划状态；
2. 调用 `LoopPlanner` 或 `TeamPlanner`，它们不调模型，只返回固定的一步；
3. `Executor.accept_plan` 安装这一步；
4. `StepExecutor.execute`：生成执行令牌，构建上下文，运行执行器，校验并规范化输出，提交结果；
5. `TaskState.complete_task`，最后由 `_result` 生成结果。

循环路径的 `max_replans` 被固定为 0（第 3 阶段），所以“重新规划”永远走不到，只会变成上限失败。实际起作用的宿主保证如下，4b 必须原样保留：

| 保证 | 现在在哪 | 循环路径上的具体表现 |
|---|---|---|
| 一次只跑一个 | `AgentRuntime._lock` | 同一任务并发调用 `run` 时排队 |
| 重新绑定作废 | `_BoundRunner.invalidate` | 重新绑定后，旧绑定迟到的输出或失败抛 `StaleExecutionError` |
| 取消后不提交 | `_BoundRunner.run` | 请求取消后，执行器即使返回结果或失败，也一律当作取消 |
| 版本校验 | `StepExecutor` 的执行令牌 | 执行器运行期间任务状态被别处改动，就抛 `StaleExecutionError` |
| 预算 | `RunMeter.budget_problem`，`meter.bound` | 执行前预算已用完，失败类型为 `budget`；单步时限不超过剩余时间 |
| 单步时限 | `asyncio.timeout(step_timeout)` 加 `pausable_deadline` | 时限 = max(服务的单步时限, 执行器时限 + 5)；等用户确认的时间不计入（D-061） |
| 副作用不重放 | `SideEffectWatch` | 写入或运行抛异常时失败类型为 `side_effect`；超时前已写入或运行，同样是 `side_effect` |
| 执行次数上限 | `max_executions` | 跨续跑累计，达到上限失败类型为 `limit` |
| 失败分类 | `_run` 里的各个 `except` | 见 9.4 的对照表 |
| 输出校验 | `StepExecutor._validate_runner_output` | 内容去掉首尾空白，来源编号必须是资源表里登记过的 |
| 追踪和事件 | `root_span`、`trace_scope('plan'/'step')`，以及 `plan`、`step` 事件 | 网页执行树、追踪查看器和评测的 `steps` 字段都读它们 |

### 9.2 新增的状态：`app/models/run_state.py`

`TaskState` 的状态机要求先有计划才能完成任务（`complete_task` 要求所有步骤都已完成），所以不能直接用在没有计划的循环上。新增两个小类：

- `RunState`：一个循环任务的权威状态。字段：
  - `task_id`、`user_id`、`active_goal`、`goal_revision`（固定为 1，循环不支持中途改目标）；
  - `status`：沿用 `TaskStatus`，只用到 CREATED、RUNNING、BLOCKED、COMPLETED、FAILED 五个值；
  - `state_version`；
  - `constraints`、`pinned_contexts`：沿用 `Constraint`、`PinnedContext`；
  - 问题列表：沿用 `OpenQuestion`，放在一个只有 `open_questions` 字段的 `scratchpad` 视图里；
  - 失败记录：只留最后一条消息。

  方法只有服务层和循环用到的：`pin_content`、`add_constraint`、`resolve_question`、`fail_task`、`start`、`block`、`complete`、`snapshot`。字段名和异常类型都与 `TaskState` 一致，例如在已完成的任务上调用 `pin_content`，同样抛 `InvalidStateTransitionError`。
- `RunMemory`：只有 `task_state`（即 `RunState`）和 `resources`（沿用 `Resources`）两个属性。服务层、`open_tool_registry` 和 `KnowledgeRegistryTool` 都只读这两个属性，所以它们不用改。

### 9.3 新增的运行：`app/agent/loop_run.py`

`LoopRun(state_memory, model, registry, *, runner_factory, runner_id, step_description, max_executions, step_timeout_seconds, context_manager)`：

- 构造和 `rebind(planner, model, registry, *, context_manager)` 的签名与 `AgentRuntime` 相同（`planner` 参数忽略），绑定逻辑直接复用 `_BoundRunner` 和 `SideEffectWatch`。
- `run(*, on_event=None)`：
  1. 加锁；挂上 `CallbackHook`、`root_span`；发出 `task_start` 事件。
  2. 进入计量范围：用调用方的计量，没有就新建一个。
  3. 如果有未回答的阻塞问题，返回 BLOCKED（与现在的 `pause_for_questions` 分支相同）。
  4. 如果预算已用完，失败，类型为 `budget`。
  5. 执行次数达到上限，失败，类型为 `limit`。
  6. 发出 `plan` 事件，并开一个 `plan` 区间：
     - 首次运行时 `revision` 为 1；
     - 续跑时为 2，与现在“回答后重新规划”的事件逐字相同；
     - `steps` 是 `LoopPlanner.STEP` 或 `TeamPlanner.STEP`，`step_ids` 是这一轮新生成的步骤编号。
  7. 执行次数加 1，重置副作用监视，开 `step` 区间，在时限和 `pausable_deadline` 里运行绑定的执行器。传给执行器的 `StepExecutionContext` 和现在 `StepExecutor.build_context` 生成的逐字段相同，包括 `current_step`、`plan_revision`、`final_step=True`。
  8. 执行器返回后：
     - 用与 `StepExecutor._validate_runner_output` 相同的规则校验并规范化输出；
     - 检查状态版本：自己写入之外，状态还被改过，就抛 `StaleExecutionError`；
     - 再按结果走成功、阻塞或失败；
     - 记录步骤日志，发出 `step` 事件。
  9. 生成 `AgentRunResult`：`steps` 里的回执字段与现在相同，`execution_count` 跨续跑累计，`replan_count` 为 0，`usage` 和 `step_log` 照旧。
  10. 发出 `task_end` 事件。
- 不支持 `AgentRuntime` 的 `conversation=` 参数。那是给底层调用的兼容入口，服务层不传，循环路径也从未用过。
- 不再固定 `available_tools` 工具清单：循环的 `_context_message` 本来就把它过滤掉了，请求内容不变。

### 9.4 结果对照（必须与现在逐字一致）

| 情况 | 现在的结果 | 4b |
|---|---|---|
| 纯文本完成 | COMPLETED，内容为去掉首尾空白的答案 | 同 |
| `ask_user` | BLOCKED，内容为问题；登记一个阻塞问题，`step_id` 为当前步骤 | 同 |
| 续跑 | 回答写入问题后再运行一次：`plan` 事件的 `revision` 为 2，`execution_count` 为 2 | 同 |
| `ToolRoundLimitError` 或其他 `StepRunError`（含不可用工具） | FAILED `limit`，“Agent replanning limit reached; last failure: …”，末尾是失败记录（类型前缀规则与 `StepExecutor` 相同） | 同 |
| 读取类工具抛异常 | 同上，失败类型为 `limit` | 同 |
| 写入或运行抛异常 | FAILED `side_effect` | 同 |
| `StepFormatError` | FAILED `format` | 同 |
| `StepModelError`（含 `TeamError`、超时） | FAILED `model` | 同 |
| 规则拒绝调用 | FAILED `policy` | 同 |
| 团队超时 | FAILED `timeout`，“Team execution timed out; …” | 同 |
| 上下文溢出恢复超时 | FAILED `timeout` | 同 |
| 超时前已写入或运行 | FAILED `side_effect` | 同 |
| 只读工具运行时超时 | FAILED `limit`，末尾带超时说明 | 同 |
| 执行器契约错误等其他异常 | FAILED `internal`，“Execution failed: 类型名” | 同 |
| 取消 | 抛 `CancelledError`，任务状态记为失败 | 同 |
| 重新绑定后迟到的结果 | 抛 `StaleExecutionError` | 同 |

### 9.5 怎么证明一致：新旧路径逐项对照

4b 期间旧路径还在（规划模式要用），所以可以把同一个替身模型脚本分别交给两条路径跑，逐项比较。新增 `tests/test_loop_run_equivalence.py`：

- 旧路径：`AgentRuntime` 加 `LoopPlanner`/`TeamPlanner`，执行器工厂和限额与 `RunAssembler` 现在给的相同。
- 新路径：`LoopRun` 加 `RunMemory`。
- 比较的内容：
  - 结果的 `status`、`content`、`failure_kind`、`execution_count`、`replan_count`；
  - `step_log`，去掉耗时和编号；
  - 回执，去掉编号和版本；
  - `task_start`、`plan`、`step`、`task_end` 事件序列，以及区间种类，同样去掉编号和时间；
  - 发给模型的每一次请求（消息和工具列表）。
- 场景：9.4 表里的每一行，再加上单一循环开 `--delegate`、团队 `require_passing_run`、只读任务、`max_executions` 达到上限、预算用完、等待确认的时间不计入时限。

这组测试是 4b 的主要验收依据。现有测试不改也要全部通过：它们一部分仍测旧路径，另一部分经服务层，会自动走新路径。

### 9.6 接到服务层

- `RagAgentService._open_task`：新任务在 `options['runner'] == 'loop'` 或 `options['mode'] == 'team'` 时建 `RunMemory`，否则建 `WorkingMemory`。`auto` 加 `plan` 的组合可能路由到团队，这时仍用 `WorkingMemory`，走旧路径，4c 删除规划模式后这个组合就不存在了。
- `RunAssembler.run`：内存是 `RunMemory` 且执行方式是团队或单一循环时，建 `LoopRun`；否则照旧建 `AgentRuntime`。规划器不再构造（`LoopPlanner`/`TeamPlanner` 只剩步骤文字，留到 4c 再删）。
- 续跑、`_live_blocked`、`resumable` 读的 `runtime.memory.task_state.status` 和 `scratchpad.open_questions`，在 `RunState` 上同样存在，不用改。

### 9.7 实现分块（每块单独测试，可以单独交给 Codex）

1. **4b-1** `run_state.py`，加单元测试。不接线，不改任何现有文件。
2. **4b-2** `loop_run.py`，加 9.5 的对照测试。不接线。
3. **4b-3** 服务层和 `RunAssembler` 接线；现有测试全部通过；跑 `init.ps1`。
4. **评测**（需要你授权）：调参集单一循环 11 次，团队 11 次（`--write --sandbox`，同 3b）。

每块派给 Codex 时都挂监视，20 分钟没有新日志就收下已完成的部分，剩下的由 Claude 接手。

### 9.8 这次不做

- 删除 `planner.py`、`executor.py`、`step_executor.py`、`tool_calling_step_runner.py`、`TaskState` 的计划状态机，以及续跑时 `revision` 变 2 这个奇怪的事件：都留到 4c。
- 循环中途改目标（`goal_revision` 大于 1）：现在循环路径也不支持，保持不支持。

## 10. 4b 实现记录（2026-11-11，stage4b-001）

用户的四个未提交文件开工前后 sha256 一致；`runtime.py` 未改动。

- **4b-1**（Codex，8 分钟，没有卡住）：新增 `app/models/run_state.py`（`RunState`、`RunStateSnapshot`、`RunMemory`）。异常类型和文字与 `TaskState` 同名方法逐字相同；回答完最后一个阻塞问题后，状态由 BLOCKED 回到 READY。新增 `tests/test_run_state.py`。
- **4b-2**（Codex，12 分钟，没有卡住）：
  - 新增 `app/agent/loop_run.py` 的 `LoopRun`，复用 `runtime.py` 的 `_BoundRunner`、`AgentRunResult`、失败分类辅助函数和 `SideEffectWatch`；输出校验从 `StepExecutor` 复制过来，没有导入 `step_executor`。
  - `RunState.block` 补记 `Observation`，和旧路径一致：重新开始时上下文里有 `last_observation`。
  - 新增 `tests/test_loop_run_equivalence.py`，67 项新旧路径对照测试，比较结果、步骤日志、回执、完整事件序列和每一次模型请求；新增 `tests/test_loop_run.py`，14 项单元测试。
  - Claude 逐行审查：成功后再查预算、超时时先在内部取消再在外部分类、三条路径上的版本校验、续跑时 `revision` 为 2，都与旧代码一致。
- **对照测试确认、并按原样保留的旧行为**（4c 再决定要不要改）：
  - 规划前执行次数用尽，失败类型是 `internal`；执行前的另一处检查，失败类型是 `limit`；
  - 成功提交后还会再查一次预算，所以可能保留了成功回执，任务最终却是 `budget` 失败；
  - 执行期间资源选择发生变化，失败类型是 `internal`；
  - 回答问题后续跑，`revision` 为 2。
- **4b-3**（Claude）：
  - `RagAgentService._open_task`：新任务在 `runner == 'loop'` 或 `mode == 'team'` 时建 `RunMemory`。
  - `RunAssembler.run`：内存是 `RunMemory` 时建 `LoopRun`（`step_description` 取 `LoopPlanner.STEP` 或 `TeamPlanner.STEP`，限额和执行器工厂不变），不再构造规划器；否则照旧建 `AgentRuntime`。服务层传入 `loop_run_factory=LoopRun`，供测试替换。
  - 有 4 项现有测试必须跟着改，原因相同：它们用替身顶替服务层的 `AgentRuntime`，来检查团队或循环是怎么构造的，现在构造的是 `LoopRun`。替身同时顶替 `LoopRun`，原来查规划器类型的断言改成查 `step_description`：
    - `tests/test_delegation.py::test_service_uses_delegating_loop_factory_and_one_step_runtime`；
    - `tests/test_route_selection.py::test_fixed_modes_keep_their_runner_and_log_a_user_decision`；
    - `tests/test_run_options.py` 里两项（替身夹具共用）；
    - `tests/test_web_loop_runner.py::test_explicit_team_does_not_use_loop_even_if_runner_choice_is_loop`。
  - `tests/test_run_assembler.py` 新增 1 项：确认单一循环和团队用 `LoopRun` 加 `RunMemory`，规划模式用 `AgentRuntime` 加 `WorkingMemory`。
- `init.ps1` 结果：2084 项通过、16 项跳过。
- 评测见第 11 节。未提交。

## 11. 4b 评测（2026-11-11，用户“跑评测”）

真实模型，每题 1 次，参数与 3b 相同。单一循环只交答案；团队写文件并开沙盒。两组同时运行，所以耗时偏长。输出：`output/bench-tune-loop-4b.json`、`output/bench-tune-team-4b.json` 和对应的 `traces-*`。

| | 4b | 3b |
|---|---|---|
| 单一循环通过 | 9/11 | 10/11 |
| 单一循环令牌 / 调用 | 502,898 / 25 | 606,738 / 28 |
| 团队通过 | 9/11 | 11/11 |
| 团队令牌 / 调用 | 1,571,633 / 78 | 1,837,245 / 78 |

通过数没有达到第 3 节的标准。两种模式都失败在同样两道题、同样两项检查上：`config_merge_qa` 的 `top_keys`，和 `todo_html` 的 `enter_key_and_trim`。

判断为服务商一侧的波动，不是 4b 造成的，依据有三条：

1. **失败的运行少了固定开销。** 平时每次请求约有 2 万令牌的固定开销（D-063），失败的这几次运行，第一次请求只有约 6,400 提示令牌：
   - 单一循环和团队的 `config_merge_qa`，分别是 6,396 和 6,375（3b 通过时是 27,545 和 27,121）；
   - 团队的 `todo_html` 是 6,360（3b 是 20,771）。
   追踪记下的回复模型名都是 `gpt-6.1-sol`，没有变化。D-072 记录过同样的现象。
2. **4b 之前就出现过。** 按需委派那轮评测（`a0aa489`，当时还没有 4b 的代码），`config_merge_qa` 第一次请求也是 6,420 提示令牌，同样以 `top_keys` 失败。
3. **请求本身没变。** 其余各题第一次请求的提示令牌与 3b 相差不到 0.5%（例如 `billing_qa` 是 20,783 对 20,784）；67 项对照测试也证明，新旧路径发给模型的请求逐字相同。

单一循环的 `todo_html`，第一次请求是正常的 20,165 令牌，失败的检查项与 3b 相同（3b 那次也没过），属于这道题本身的波动。

结论：4b 的代码改动，在调参集上看不出回退。如果要拿可比的通过数，可以在提示开销正常时，单独重跑 `config_merge_qa` 和 `todo_html` 两种模式各 1 次，共 4 次真实调用。

### 11.1 重跑两题（2026-11-11，用户“重跑两题”）

`config_merge_qa` 和 `todo_html` 在两种模式下各重跑 1 次，依次运行，参数同上。输出：`output/bench-rerun-loop-4b.json`、`output/bench-rerun-team-4b.json` 和对应的 `traces-rerun-*`。

| | 通过 | 令牌 / 调用 | 第一次请求的提示令牌 |
|---|---|---|---|
| 单一循环 `config_merge_qa` | 是 | 5,442 / 2 | 2,175 |
| 单一循环 `todo_html` | 是 | 43,797 / 2 | 20,159 |
| 团队 `config_merge_qa` | 是 | 11,036 / 4 | 2,163 |
| 团队 `todo_html` | 否（`enter_key_and_trim`） | 326,303 / 12 | 20,773 |

**更正上面第 1 条判断。** 这次 `config_merge_qa` 两种模式第一次请求的提示令牌更低（约 2,200），却都通过了。可见固定开销缺失只是服务商一侧的另一种状态，和 `top_keys` 失败没有因果关系；上面把失败归因于开销缺失，依据不足。较稳妥的说法是：这两项检查本身就不稳定，第 2、3 条依据（4b 之前就出现过；新旧路径请求逐字相同）仍然成立。

团队的 `todo_html` 这次提示开销正常，仍以 `enter_key_and_trim` 失败；这一项在 3b 单一循环、开沙盒的单一循环评测（loop-sandbox-check-001）里都失败过，属于这道题本身的波动。

合并计算（原 9 题加重跑的 2 题）：单一循环 11/11，团队 10/11。结论不变：4b 在调参集上看不出回退。


## 12. 4c 详细设计（2026-11-11，用户“写设计吧，可以调用codeX”）

状态：**设计稿，等你确认 12.9 的问题和第 13 节的测试清单；未改代码**（stage4c-001）。
依据：当前代码 `70f7470`（4b 已提交）。测试逐项分类原本派给 Codex 做只读盘点，它中途用完额度（11 月 15 日恢复），改由 Claude 完成，结果见第 13 节。

### 12.1 原则

- **发给模型的请求逐字不变。** 4c 只删除不再走到的代码，不改循环和团队的行为。`StepExecutionContext` 的字段、`plan`/`step` 事件的形状、续跑时 `revision` 为 2、第 10 节列出的几处怪行为，都原样保留。这样 4c 的评测可以直接和 4b 比；要改这些行为，另开一步。
- **能缩就不另起名字。** 保留模块名、只删内容，可以少改导入。只有被删的模块里还有要留的东西时，才搬到别处。
- **你的三份笔记**：`planner.py`、`step_executor.py`、`tool_calling_step_runner.py` 整个删除。开工前你先自己保存，确认后才动；实现时再核对一次这三个文件的哈希，和第 10 节记下的一致才删。

### 12.2 删除的文件

| 文件 | 行数 | 说明 |
|---|---|---|
| `app/agent/planner.py` | 423 | `Planner`、`PlannerContext`、`PlanProposal` 和规划提示 |
| `app/agent/executor.py` | 92 | `Executor`、`StalePlanError`、`PlanAcceptance` |
| `app/agent/step_executor.py` | 280 | `StepExecutor`；`LoopRun` 已有自己的一份输出校验（4b 从这里复制） |
| `app/agent/tool_calling_step_runner.py` | 469 | 规划模式逐步调用工具的执行器；循环和团队都不用它 |
| `app/agent/planning_model.py` | 24 | 只给规划器用 |
| `app/models/working_memory.py` | 228 | `WorkingMemory` 及其执行令牌、提交结果；`StaleExecutionError` 搬走（见 12.3） |

以上合计约 1,500 行。

### 12.3 缩小的文件

- `app/agent/runtime.py`：删除 `AgentRuntime` 和 `_tool_calling_factory`。保留 `AgentRunResult`、`FAILURE_MESSAGES`、`_policy_denial`、`_int_or_none`，`loop_run.py` 和服务层仍从这里导入，模块说明改为“任务结果与失败分类”。`_BoundRunner` 搬进 `loop_run.py`，只有它用。
- `app/agent/loop_run.py`：删除只为和 `AgentRuntime` 对齐而留的兼容参数：构造参数 `max_replans`、`planning_timeout_seconds`，以及 `rebind` 的 `planner` 参数；`rebind` 改为 `rebind(model, registry, *, context_manager=None)`。
- `app/agent/agent_loop.py`：删除 `LoopPlanner`、`TeamPlanner`，以及对 `planner.py` 的导入。两段步骤文字改为模块常量 `LOOP_STEP`、`TEAM_STEP`，文字不变。
- `app/agent/step_runner.py`：删除 `StepRoutingError`；`loop_run.py` 的失败分类里同时去掉它。现在没有任何地方抛出它。
- `app/models/taskstate.py`：删除 `TaskState`、`TaskStateSnapshot` 两个类，约 700 行。保留值类型，因为 `RunState`、`step_models.py`、`LoopRun`、网页和命令行都还在用：`TaskStatus`、`StepStatus`、`PlanStep`、`IntermediateResult`、`Observation`、`OpenQuestion`、`FailureRecord`、`Scratchpad`、`PinnedContext`、`Constraint`、`InvalidStateTransitionError`。
- `app/models/run_state.py`：接收 `StaleExecutionError`（原在 `working_memory.py`），并删除 `RunState._check_planning_change` 里与计划有关的说明。
- `app/tools/tool_loader.py`、`app/tools/knowledge_registry_tool.py`：类型标注从 `WorkingMemory` 改为 `RunMemory`。它们本来就只读 `task_state` 和 `resources`，行为不变。
- `app/agent/mcp_demo/web_search_client.py`：改用 `AgentLoopRunner` 加 `LoopRun` 运行一次联网搜索；`tests/tools/test_web_search_integration.py` 导入它，所以要保留并改写。见 12.9 第 2 问。

### 12.4 服务层和入口

- `RagAgentService`：
  - 删除构造参数 `planner_model` 和 `max_replans`；`single_runner` 的默认值改为 `'loop'`，只接受 `'loop'`（传 `'plan'` 抛 `ValueError`，说明规划模式已在 4c 删除）。
  - `_open_task` 一律建 `RunMemory`；`_models` 只产出一个模型，不再包 `PlanningModel`。
  - `capabilities()` 的 `runners` 变为 `['loop']`，`default_runner` 为 `'loop'`。字段保留，旧前端也能用。
  - `self._tasks` 的类型从 `AgentRuntime` 改为 `LoopRun`。
- `RunAssembler`：删除 `prepare_planner_model`、`planner_model_injected` 参数、`runtime_factory` 参数和 `AgentRuntime` 分支，只剩 `LoopRun`；续跑时仍调用 `rebind`。
- 命令行 `python -m app.agent`、网页后端 `python -m app.api`、评测 `bench.run_bench`：
  - `--single-runner` 只剩 `loop` 一个选项，暂时保留，旧命令还能用，下一阶段再删；
  - 命令行删除 `--max-replans`。
- 网页：
  - `app.py` 请求里的 `runner` 改为只接受 `'loop'`，传 `'plan'` 返回 422；
  - `index.html` 删除“按计划”选项。`app.js` 在只有一个执行方式时本来就会隐藏下拉框（第 374 行），不用改；
  - 第 344 行“旧服务端没有执行方式就当作规划模式”的兜底也删掉。
- 评测报告的 `runner` 字段保留，值固定为 `loop`，旧报告仍可对比。

### 12.5 不改的

- 循环和团队的提示、预算、完成门槛；按需委派默认关闭；`route_selection` 的 `auto` 路由。
- `StepExecutionContext`、`StepRunOutput` 等 `step_models.py` 的数据结构，以及 `plan`/`step` 事件、追踪区间、`AgentRunResult` 的字段。网页执行树、追踪查看器、评测的 `steps` 字段都不用改。
- hooks、副作用保护、`RunMeter`、对话归档、长期记忆、工具注册表和 MCP。
- `.env`、模型默认值、依赖。

### 12.6 `AGENTS.md`、决策和文档

- `AGENTS.md` 全局不变量改两条：
  - 第一条改为：“一个任务一个循环：`LoopRun` 负责取消、超时、重新绑定、版本校验、预算和结果提交；执行器（`AgentLoopRunner`）只返回输出，不直接改权威状态。”
  - “ConversationMemory 管会话视图，WorkingMemory 管任务真值”改为“`RunState` 管任务真值”。
- `docs/DECISIONS.md` 新增 D-076：删除规划模式和规划—执行骨架。写明原因（默认不用，评测不占优，见 D-072）、保留了哪些宿主保证、事件和请求不变、哪些旧名字删了。
- `CLAUDE.md` 架构概览改写。现在还写着 `team_v2.py`、`runner_registry.py` 等 3b 已删的文件，这次一并按现状改。
- `docs/agent_runtime.md` 开头的流程图和职责说明改为 `LoopRun`。
- `docs/DOCUMENT_INDEX.md`、`feature_list.json`（新增 stage4c-001）、`progress.md`、`session-handoff.md` 照常登记。

### 12.7 4b 对照测试怎么办

`tests/test_loop_run_equivalence.py` 拿旧路径 `AgentRuntime` 当标准，旧路径删了就没法再对照。做法：

1. **删除前先定标准答案**：在当前代码上，把每个场景旧路径的结果、步骤日志、回执、事件序列和模型请求序列化成固定的期望值文件，放在 `tests/fixtures/loop_run_golden/`，每个场景一个文件，去掉编号和时间。
2. 改写对照测试：只跑 `LoopRun`，和期望值文件逐项比较。
3. 生成期望值的脚本只在第 1 步用一次，不进仓库；期望值文件进仓库。

这样 4b 证明过的“新旧一致”，在 4c 删除旧路径之后仍然有测试守着。

### 12.8 实现分块（每块单独测试，交给 Codex，挂监视）

1. **4c-1 定标准答案**：生成期望值文件，把对照测试改为只跑 `LoopRun`，并对照期望值。这时旧路径还在，可以先确认改写后的测试在新旧两边都通过。不删任何东西。
2. **4c-2 迁移测试**：第 13 节里标为“迁移”的测试改到循环路径上，标为“机械修改”的按说明改，补上第 13 节最后列出的、目前只在旧路径上测过的宿主保证。这时旧路径仍在，所有测试都应通过。
3. **4c-3 删除**：先核对你那三个文件的哈希，再删 12.2 的文件，缩小 12.3 的文件，改 12.4 的服务层和入口，删除第 13 节里标为“删除”的测试；跑 `init.ps1`。
4. **4c-4 文档**：12.6 的各项，Claude 来做。
5. **评测**（要你授权）：调参集单一循环 11 次、团队 11 次（`--write --sandbox`），不低于 4b（11/11、10/11，按合并计算）；留出集单一循环 13 次，不低于 10/13。

每块做完由 Claude 审查后，再派下一块。

### 12.9 需要你确认的问题

1. **测试清单**：第 13 节的删除、迁移、修改清单，可以吗？
2. **联网搜索演示**：`app/agent/mcp_demo/web_search_client.py` 用的是要删的 `StepExecutor` 和 `ToolCallingStepRunner`。可选：
   - (a) 改写成用循环跑一次搜索，保留它的集成测试；
   - (b) 连同它的集成测试一起删除。README 已经不提它了。

   建议 (a)，因为它是唯一一个用真实 stdio MCP 跑完整执行器的测试。
3. **由谁实现**：Codex 额度用完了，要到 11 月 15 日才恢复。可选：
   - (a) 等额度恢复，再交给 Codex；
   - (b) 由 Claude 现在实现，仍按 12.8 分块，每块做完给你看结果。

   4c-2 要改约 185 个测试函数，是第 4 阶段最大的一块。
4. **你的三份笔记**：开始 4c-3 之前，请告诉我你已经保存好了。4c-1、4c-2 不删任何东西，可以先做。

**用户回答（2026-11-11）**：第 1 问“可以”；第 2 问选 (a) 改写成循环；第 3 问选 (b) 由 Claude 现在实现；第 4 问在 4c-3 前再确认。

## 13. 4c 测试清单（2026-11-11，先给你看，同意后再动）

**用户回答（2026-11-11）**：“可以”，同意本节清单。

### 13.1 怎么得出的

- 正则扫描看不到辅助函数和夹具里间接用到的骨架，所以改用实测：在当前代码上跑一遍全部测试，用一个只在本机用的 pytest 插件，记录每项测试实际构造了哪些骨架类（`Planner`、`Executor`、`StepExecutor`、`ToolCallingStepRunner`、`AgentRuntime`、`WorkingMemory`、`TaskState`）。
- 结果：共 997 个测试函数（2,100 项，含参数化），其中 348 个函数、分布在 48 个文件里，实际碰到了骨架。
- 再逐个文件读测试，判断属于哪一类：
  - **删除**：只测规划模式内部；或者循环路径上已有测同一件事的测试，注明是哪一项；
  - **迁移**：测的是宿主保证或共用机制，但只经过旧路径，要在循环或 `LoopRun` 上重写；
  - **修改**：只是因为服务层默认走规划模式，或者用 `WorkingMemory`、`TaskState` 当搭架子的材料；意图不变，换成循环再跑；
  - **不动**：只用到保留下来的名字。
- 另有少量测试没碰骨架，但断言了 `'plan'` 这个选项，例如 `runners == ['plan', 'loop']`。它们在 4c-3 改了服务层后会失败，届时按“修改”处理，不另列。

合计约：**删除 153 个函数，迁移 14 个，修改约 185 个**，其余不动。

### 13.2 整个删除的文件（13 个，128 个函数；另有 `test_runtime.py` 删 19 个、迁移 1 个）

| 文件 | 函数 | 原因；循环路径上测同一件事的测试 |
|---|---|---|
| `tests/agent/test_planner.py` | 17 | 规划上下文、计划校验、规划提示、重新规划 |
| `tests/agent/test_planner_repair.py` | 11 | 计划 JSON 的格式修复和规划超时重试 |
| `tests/agent/test_executor.py` | 7 | `Executor.accept_plan` 和过期提议 |
| `tests/agent/test_working_memory_v1.py` | 11 | 计划安装、步骤执行中改需求、资源选择和计划的配合 |
| `tests/agent/test_step_executor.py` | 8 | `StepExecutor` 提交；输出校验的 4 项已由 `test_loop_run_equivalence.py::test_output_validation_matches_legacy` 覆盖 |
| `tests/agent/test_step_outcomes.py` | 2 | 步骤结果的分发；由对照测试 `test_model_scenarios` 覆盖 |
| `tests/agent/test_final_step.py` | 3 | 只有最后一步带收尾规则；循环只有一步 |
| `tests/agent/test_tool_calling_step_runner.py` | 16 | 规划模式的执行器。循环的对应测试在 `test_agent_loop.py`：预算用完先请模型收尾、不可用工具、工具抛异常、空回复 |
| `tests/agent/test_runtime.py` | 20 | 规划、重新规划、多步计划。取消、超时、执行次数上限、阻塞后续跑，由对照测试 `test_cancellation_never_commits`、`test_host_boundaries`、`test_exact_execution_limit_can_still_complete`、`test_model_scenarios` 覆盖。删除 19 个；`test_cancellation_with_real_mcp_session_closes_tools`（真实 MCP 会话，取消后工具连接关闭）迁移到 `LoopRun`，之后整个文件删除 |
| `tests/models/test_execution_commit.py` | 7 | `WorkingMemory` 的执行令牌和提交 |
| `tests/models/test_taskstate.py` | 11 | `TaskState` 的计划和步骤状态机；`RunState` 有自己的 `test_run_state.py` |
| `tests/models/test_working_memory.py` | 7 | `WorkingMemory` 的组合和资源移除；资源移除在应用代码里没有调用方 |
| `tests/test_step_tool_budget.py` | 4 | 规划模式执行器的收尾；循环对应 `test_agent_loop.py::test_a_spent_budget_asks_once_for_the_answer_then_fails`、`test_host_calls_in_one_batch_obey_call_budget` |
| `tests/test_step_tool_list.py` | 5 | 规划模式的工具清单固定；循环对应 `test_agent_loop.py::test_the_context_skips_the_duplicated_tool_list_and_empty_fields` |

### 13.3 部分删除、部分迁移的文件

| 文件 | 删除 | 迁移到循环 / `LoopRun` | 其余 |
|---|---|---|---|
| `tests/agent/test_execution_reliability.py`（17） | 15：格式修复 4 项（规划模式的 JSON 结果，循环没有）；不可用工具 3 项（`test_agent_loop.py::test_an_unavailable_tool_is_answered_then_bounded` 覆盖）；读写抛异常和超时 4 项、失败分类 3 项（对照测试 `test_host_boundaries`、`test_model_scenarios` 覆盖）；回答问题不占重新规划次数 1 项 | `test_the_step_deadline_outlasts_a_sandbox_call` | `test_every_failure_kind_has_a_chinese_message` 不动（`FAILURE_MESSAGES` 保留） |
| `tests/agent/test_context_integration.py`（5） | 2：规划器的上下文预算 | 3：必需上下文超预算时在调模型之前失败、不运行工具；溢出后重试不重复执行已完成的工具；溢出恢复超时不重放（实现时若发现对照测试已逐项覆盖，就改为删除） | — |
| `tests/agent/test_observability.py`（16） | — | 5：结果带用量和不含工具原文的步骤日志；失败只记类型；令牌预算在两次模型调用之间生效；时间预算限制慢步骤；没有外部计量时每次运行单独计量、已完成的任务重复返回结果 | 11 项不动 |
| `tests/test_tool_argument_repair.py`（5） | — | 3：工具参数修复，以及修复有上限、只修参数错误。循环里有同样的修复（`agent_loop.py` 的 `arg_repairs`），但目前只有团队成员的测试经过它 | 2 项（适配器）不动 |
| `tests/test_prompt_cache_layout.py`（6） | — | 1：稳定的上下文字段排在前面，改到循环的 `_context_message` 上 | 5 项不动 |
| `tests/agent/test_blocked_and_resume_prompts.py`（3） | 2：规划模式执行器的提问提示；循环由 `test_agent_loop.py::test_ask_user_blocks_with_the_question` 覆盖 | — | 第 3 项按“修改”处理 |
| `tests/test_loop_run.py`（7） | 2：`test_format_repairs_and_rebind_ignore_planner`、`test_planning_compatibility_parameters_do_not_enable_replay`（兼容参数删除，见 12.3） | — | `test_validation_matches_runtime` 按“修改”：改为固定的期望错误 |
| `tests/test_trace.py`（27） | 1：`test_a_two_step_plan_run_is_a_tree_of_steps`（多步计划） | — | 6 项按“修改” |
| `tests/test_web_loop_runner.py`（14） | 2：`test_loop_override_does_not_change_the_next_tasks_plan_default`、`test_plan_resume_ignores_new_loop_choice` | — | `test_each_task_can_select_runner_or_keep_service_default` 改为“只能选循环，传 `plan` 被拒绝” |
| `tests/test_run_assembler.py`（7） | 1：`test_plan_mode_keeps_the_service_limits` | — | 5 项按“修改” |

### 13.4 修改的文件（意图不变，换到循环路径）

**用 `RunMemory(RunState(...))` 替换 `WorkingMemory(TaskState(...))`，或者直接运行执行器、不再经过 `StepExecutor`：**
- `tests/test_agent_loop.py`（19 个）、`tests/test_loop_delegate.py`（9 个）、`tests/test_delegation.py`（38 个）：这些测试本来就在测循环，只是借 `StepExecutor` 加 `WorkingMemory` 当测试架子。改为用 `LoopRun` 跑，或者直接构造 `StepExecutionContext` 调用执行器。
- `tests/test_harness_smoke.py`（2 个）：`init.ps1` 要求这个文件存在。改为 `RunState` 的最小冒烟测试：开始 → 完成；完成后再写入被拒绝且状态不变。
- `tests/tools/test_context_history.py`（2 个）、`tests/tools/test_tool_loader.py`（3 个）：只把记忆当参数传进去。
- `tests/tools/test_web_search_integration.py`（2 个）：跟着演示脚本改，见 12.9 第 2 问。

**定成固定期望值：**
- `tests/test_loop_run_equivalence.py`（12 个，67 项）：见 12.7。
- `tests/test_run_state.py`（7 个）：现在拿 `TaskState` 当标准比较异常类型和文字，改为直接写出期望值，值取自当前 `TaskState` 的实际输出。

**服务层测试：去掉 `planner_model`，替身模型改为按循环的方式回复：**

现在这些测试给规划器一份计划 JSON，再给执行器一份步骤结果 JSON；改为给一个模型一串 `ModelTurn`，比如“调一次工具，再回答”。实现时先加一个共用的替身模型 `tests/loop_fakes.py`，各文件尽量改用它，避免每个文件各写一份。

| 文件 | 个数 | 文件 | 个数 |
|---|---|---|---|
| `tests/test_turn_lifecycle.py` | 12 | `tests/test_run_options.py` | 10 |
| `tests/test_web_api.py` | 9 | `tests/test_web_approvals.py` | 8 |
| `tests/test_workspace.py` | 7 | `tests/test_progress_and_history.py` | 7 |
| `tests/test_trace.py` | 6 | `tests/test_builtin_hooks.py` | 5 |
| `tests/test_route_selection.py` | 5 | `tests/test_run_assembler.py` | 5 |
| `tests/test_hooks.py` | 4 | `tests/agent/test_runtime_service.py` | 3 |
| `tests/test_longterm_memory_wiring.py` | 2 | `tests/agent/test_persistent_service.py` | 2 |
| `tests/test_summary_scheduler.py` | 2 | `tests/test_summary_context.py` | 1 |
| `tests/agent/test_summary_lifecycle_e2e.py` | 1（10 项参数化） | `tests/agent/test_blocked_and_resume_prompts.py` | 1 |

其中有两项要改写，不只是换替身：
- `test_turn_lifecycle.py::test_service_blocked_task_resumes_in_new_turn_without_repeating_completed_step`：原来测“多步计划续跑时不重做已完成的步骤”，改为“循环回答问题后续跑，不重复已经执行过的工具调用”。
- `test_longterm_memory_wiring.py::test_recalled_memories_reach_the_planner_as_pinned_reference_data`：改为“召回的记忆作为固定参考资料出现在循环的上下文里”。

### 13.5 目前只在旧路径上测过的宿主保证（4c-2 要在 `LoopRun` 上补测试）

1. 单一循环的工具参数修复：修复有上限、报告错误类型链、只修参数错误。
2. 上下文预算：必需上下文超预算时在调模型之前失败；溢出后重试不重复执行已完成的工具。
3. 计量：令牌预算在两次模型调用之间生效；时间预算限制慢步骤；步骤日志不含工具原文，失败只记类型。
4. 单步时限比沙盒调用长：时限 = max(服务的单步时限, 执行器时限 + 5)。
5. 稳定的上下文字段排在请求前面（提示缓存，D-057）。
6. 用真实 stdio MCP 会话运行时被取消，工具连接随之关闭。

审计记录、网页确认、hooks 事件、追踪、工作目录、长期记忆这些服务层的保证，在 13.4 把服务层测试换到循环之后，就由原来那些测试在循环路径上守着，不用另补。

## 14. 4c 实现记录

### 4c-1 定标准答案（2026-11-11，Claude）

用户选择由 Claude 现在实现（12.9 第 3 问），Codex 额度 11 月 15 日恢复。

- `tests/test_loop_run_equivalence.py` 改为与期望值文件比较：
  - 文件放在 `tests/fixtures/loop_run_golden/`，每个测试用例一个，共 67 个、约 390 KB。内容就是 4b 对照时比较的那些：结果、步骤日志、回执、事件序列、每一次模型请求，以及两项测试里传给执行器的上下文；
  - 默认只跑 `LoopRun`，再和文件逐项比较；
  - 设 `LOOP_RUN_RECORD=1` 时，旧路径也跑，先断言新旧一致，再用旧路径的输出重写文件。这个模式只保留到 4c-3，届时连同旧路径一起删除。
- 验证：
  - 录制模式 67 项通过，即新旧仍然一致，文件由旧路径的输出写成；
  - 默认模式 67 项通过，即 `LoopRun` 与文件一致；
  - 故意把一个文件里的 `completed` 改成 `failed`，对应测试失败，改回后通过；
  - 文件里没有本机路径。
- 除了把 `assert outputs[1] == outputs[0]` 改为与文件比较，各测试原有的断言都保留。

### 4c-2 迁移和修改测试（2026-11-11，Claude）

这一块不删任何文件，旧路径还在，所有测试都要通过。

**应用代码，三处小改动，行为不变或只改库默认值：**
- `app/agent/loop_run.py`：把 `LoopRun` 构建执行上下文的代码提成模块函数 `step_context(memory, step, plan_revision)`，`LoopRun` 自己也调用它。测试用同一个函数构建上下文，不再借 `StepExecutor.build_context`。期望值文件确认行为不变。
- `app/agent/agent_loop.py`：新增模块常量 `LOOP_STEP`、`TEAM_STEP`，`LoopPlanner.STEP`、`TeamPlanner.STEP` 改为引用它们，文字不变。
- `app/services/rag_agent_service.py`：库默认值 `single_runner` 从 `'plan'` 改为 `'loop'`。原定在 4c-3 改，提前到这里，是为了不用在上百处服务层调用里逐个写 `single_runner='loop'`。命令行、网页、评测本来就默认循环；规划模式此时仍可选。
- `app/agent/mcp_demo/web_search_client.py`：改为用 `AgentLoopRunner` 加 `LoopRun` 跑一次联网搜索（12.9 第 2 问选 (a)）。

**测试：**
- 新增 `tests/loop_fakes.py`：`started_context()` 在 `RunMemory` 上开始一步并返回 `LoopRun` 会给执行器的上下文；`loop_reply()` 把规划模式的步骤结果换成循环的回复（答案本身，或者 `ask_user` 调用）。
- 新增 `tests/test_loop_run_guarantees.py`，12 项，对应 13.5 列出的宿主保证：参数修复 3 项、上下文预算 2 项、计量 5 项、请求开头稳定 1 项、真实 MCP 会话取消 1 项。
- 测试架子换掉：`test_agent_loop.py`、`test_loop_delegate.py`、`test_sandbox_step1.py::_context`（`test_delegation.py`、`test_sandbox_step3.py`、`test_trace.py` 共用它）改用 `started_context()`。
- `test_delegation.py`：基于 `AgentRuntime` 的 9 项宿主保证测试（重新绑定、取消、超时、过期结果、服务层过期清理等）改在 `LoopRun` 上跑；删除 3 项只测规划模式的：`test_plain_worker_overflows_on_whole_file_writes`（旧执行器的基线）、`test_default_factory_preserves_exact_output_and_model_messages`（规划模式的默认工厂）、`test_single_step_planner_never_calls_the_planning_model`（`TeamPlanner`）。
- 定成固定期望值：`test_run_state.py` 把 `TaskState` 的 115 条报错录成 `tests/fixtures/run_state_legacy_errors.json`，测试里不再出现 `TaskState`；`test_loop_run.py::test_validation_matches_runtime` 把 `AgentRuntime` 的 41 条参数校验报错录成 `tests/fixtures/loop_run_legacy_validation.json`。
- 服务层测试换到循环：去掉 `planner_model`，步骤结果 JSON 换成答案本身或 `ask_user` 调用；原来查规划器提示的断言，改为查循环发给模型的请求。涉及 `test_run_options`、`test_progress_and_history`、`test_web_api`、`test_turn_lifecycle`、`test_runtime_service`、`test_workspace`、`test_web_loop_runner`、`test_longterm_memory_wiring`、`test_web_approvals`、`test_summary_scheduler`、`test_persistent_service`、`test_summary_context`、`test_route_selection`、`test_hooks`、`test_builtin_hooks`、`test_blocked_and_resume_prompts`、`test_summary_lifecycle_e2e`、`test_trace`。两项按 13.4 改写了意图：
  - 续跑不重复已完成的工作：改为续跑时模型收到的消息里只有一条时钟工具的回复，没有再调用；
  - 召回的记忆出现在循环的上下文里，不再查规划器提示。
- `test_harness_smoke.py` 改为 `RunState` 的冒烟测试；`tests/tools/test_context_history.py`、`test_tool_loader.py`、`test_web_search_integration.py` 改用 `RunMemory`、`LoopRun`。
- 结果：全部测试 2093 项通过、16 项跳过（4c-1 时 2084 项：新增 12 项，删除 3 项）。

**和第 13 节清单的出入：**
- 多出 3 项要在 4c-3 删除，都只测规划模式：
  - `test_turn_lifecycle.py::test_standalone_runtime_records_input_before_execution_and_on_cancel` 和 `::test_pending_standalone_outcome_does_not_hide_resolved_question`：测的是 `AgentRuntime` 的 `conversation=` 参数，`LoopRun` 没有这个参数（9.3）；
  - `test_route_selection.py::test_single_mode_answers_with_the_last_step_result`：两步计划。
- `test_execution_reliability.py::test_the_step_deadline_outlasts_a_sandbox_call` 其实没有经过旧路径，它测的是命令行的 `step_timeout_for`，归为“不动”。所以迁移是 13 项，不是 14 项。
- `test_run_assembler.py` 的 6 项测的是 `RunAssembler` 的规划分支和 `planner_model`、`runtime_factory` 参数，留到 4c-3 和组装器一起改。
- 重新跑探针：仍碰到骨架的测试函数从 348 个降到 174 个。除了 `test_run_assembler.py` 6 项、`test_web_loop_runner.py` 1 项（选执行方式）和对照测试里 3 处传给 `rebind` 的 `LoopPlanner`（这三处都在 4c-3 跟着代码改），其余都是 4c-3 要删除的。

**发现一处原有行为，记下来，没有改：** 循环的令牌预算只在一步开始前和结束后检查，一步里面的多次模型调用之间不检查。规划模式每一步只调一次模型，所以以前看起来是“在两次调用之间停下”。现在一个循环步会把它的调用做完，然后整个任务以 `budget` 失败，成功回执保留。时间预算不受影响，单步时限会被剩余时间截短。这是 D-072 以来循环一直有的行为；`test_loop_run_guarantees.py::test_the_token_budget_is_checked_before_and_after_the_loop_step_not_inside_it` 把它固定了下来。要不要在循环里每次调用前检查，另行决定。

### 4c-3 删除（2026-11-11，Claude）

开工前用户确认“已保存”三份笔记（12.9 第 4 问）；删除前核对 `planner.py`、`step_executor.py`、`tool_calling_step_runner.py` 的哈希，与第 10 节记下的一致。这三个文件里有用户未提交的改动，`git rm` 因此拒绝删除；改为直接从工作区删除，没有暂存任何东西。

**删除的文件（12.2）**：`app/agent/planner.py`、`executor.py`、`step_executor.py`、`tool_calling_step_runner.py`、`planning_model.py`，`app/models/working_memory.py`。

**缩小的文件（12.3）：**
- `app/agent/runtime.py` 只剩 `AgentRunResult`、`FAILURE_MESSAGES`、`_policy_denial`、`_int_or_none`（474 行 → 61 行）；`_BoundRunner` 搬进 `loop_run.py`。
- `app/models/taskstate.py` 只剩值类型（928 行 → 122 行）；`StaleExecutionError` 搬进 `run_state.py`。
- `app/agent/agent_loop.py` 删除 `LoopPlanner`、`TeamPlanner`，只留 `LOOP_STEP`、`TEAM_STEP`；`step_runner.py` 删除 `StepRoutingError`。
- `LoopRun` 删除兼容参数 `max_replans`、`planning_timeout_seconds`，`rebind(model, registry, *, context_manager)` 不再收 `planner`。
- `tool_loader.py`、`knowledge_registry_tool.py` 的类型标注改为 `RunMemory`。

**服务层和入口（12.4）：**
- `RagAgentService` 删除 `planner_model`、`max_replans`、`planning_timeout_seconds`；`single_runner` 只收 `'loop'`，传 `'plan'` 报“single_runner plan was removed in stage 4c (D-076); use loop”。每个任务都建 `RunMemory`，`_models` 只给一个模型，`run_options()['runners']` 为 `['loop']`。
- `RunAssembler` 删除 `planner_model_injected`、`prepare_planner_model`、`planner_model`、`runtime_factory` 和 `AgentRuntime` 分支。
- 命令行、网页后端、评测的 `--single-runner` 只剩 `loop`；命令行删除 `--max-replans`。网页请求的 `runner` 只收 `'loop'`，传 `'plan'` 返回 400（参数校验失败，代码 `invalid_input`，与其他非法选项相同）；页面删除“按计划”选项，下拉框默认隐藏。
- `app/config.py` 的 `agent_planning_timeout_seconds` 没有删：`.env` 里可能还有这一项，删掉配置字段要另改 `.env.example`；现在没有代码读它。（2026-11-12 已删，`.env.example` 同步去掉；配置忽略多余项，旧 `.env` 里留着这一行也能正常载入。D-077）

**测试：**
- 整个删除 15 个文件：13.2 的 14 个，加上 `tests/agent/test_context_integration.py`（两项规划器测试；另两项已迁到 `test_loop_run_guarantees.py`；“溢出恢复超时不重放”由期望值文件 `test_host_boundaries_timeout-overflow` 覆盖）。
- 部分删除：`test_execution_reliability.py`（留 2 项）、`test_observability.py`（删 5 项）、`test_tool_argument_repair.py`（删 3 项）、`test_prompt_cache_layout.py`（删 1 项）、`test_blocked_and_resume_prompts.py`（删 2 项）、`test_loop_run.py`（删 2 项和兼容参数的校验用例）、`test_trace.py`、`test_route_selection.py`（各删 1 项）、`test_turn_lifecycle.py`（删 2 项）、`test_tool_result_budgets.py`（删 1 项规划提示测试，盘点时漏掉了：它只读 `Planner._build_prompt` 的源码，没有构造规划器）。
- 对照测试删除录制模式和旧路径，只和期望值文件比较：67 项全部通过，说明删除前后 `LoopRun` 的行为逐项不变。
- 改写：`test_run_assembler.py`（只剩循环）、`test_web_loop_runner.py`（只能选循环，`plan` 在服务、网页、命令行都被拒绝）、`test_hooks.py`（不再有规划模型，调用只标 `step`、`wrap_up`）、`test_stage3_cleanup.py`（检查对象从 `AgentRuntime` 换成 `LoopRun`）、`test_tool_boundaries.py`、`test_sandbox_docker.py`（真实容器测试，默认跳过，本轮没跑）。
- 新增 `tests/test_stage4_cleanup.py`：删除的模块和名字不能回来，服务层和 `LoopRun` 不再有规划参数。
- 结果：`init.ps1` 通过，1731 项通过、16 项跳过（4c-2 后 2093 项）。

**行数**（相对 `70f7470`，含 4c-2）：`app/` 24 个文件，新增 185 行、删除 2,861 行。删除的行里包括用户在三个文件里写的中文笔记，用户已自行保存。

**还没做：** 4c-4 文档（`AGENTS.md` 两条不变量、D-076、`CLAUDE.md` 架构概览、`docs/agent_runtime.md`）和评测。

### 4c-4 文档（2026-11-11，Claude）

- `AGENTS.md`：第一条全局不变量改为“一个任务一个循环：`LoopRun` 负责取消、超时、重新绑定、版本校验、预算和结果提交；执行器（`AgentLoopRunner`）只返回输出，不直接改权威状态”；“WorkingMemory 管任务真值”改为“`RunState` 管任务真值”；测试数改为 1731。
- `docs/DECISIONS.md` 新增 D-076。
- `CLAUDE.md`：架构概览按现状重写（`LoopRun`、`RunState`、`RunAssembler`，单一循环和团队），顺带去掉 3b 已删的 `team_v2.py`、`runner_registry.py` 等；示例测试命令改为 `tests/test_loop_run.py`。
- `docs/agent_runtime.md`：前半部分重写：流程图、职责、失败分类对照表（逐项对过期望值文件）、默认上限、阻塞续跑和底层示例（示例代码用替身模型跑过一次：先阻塞，回答后完成，2 次执行）；长期记忆和上下文两节只改了提到规划器的两处。
- `docs/PROJECT_HANDOVER.md` 第 1、3、5、7 节，`README.md` 的近期更改和用法，`docs/DOCUMENT_INDEX.md` 两条。
- 早于第 4 阶段的设计文档（`executor_v1.md`、`working_memory_v1.md`、`multi_runner_infrastructure.md` 等）是历史记录，原样保留。
- 基线清单刷新为 `docs/harness/history/baseline-2026-11-11-stage4c.json`（`docs/harness/baseline.md` 指向它）。用户自己的未提交差异只剩 `scripts/` 下三个脚本的删除；三个笔记文件已按用户确认删除，它们最后的哈希记在清单的 `user_deleted_after_confirmation` 里。

剩下：评测（要用户授权）：调参集单一循环 11 次、团队 11 次（`--write --sandbox`），留出集单一循环 13 次（12.8 第 5 项）。

### 4c 评测（2026-11-11，用户“跑评测”）

真实模型，每题 1 次，三组依次运行。调参集参数同 4b（单一循环只交答案，团队 `--write --sandbox`）；留出集单一循环只交答案。输出：`output/bench-tune-loop-4c.json`、`bench-tune-team-4c.json`、`bench-holdout-loop-4c.json` 和对应的 `traces-*-4c`。

| | 4c | 4b（合并重跑） | 标准 |
|---|---|---|---|
| 调参集单一循环 | 9/11（586,984 令牌 / 28 次调用） | 11/11 | 不低于 4b |
| 调参集团队 | 10/11（1,807,939 / 75） | 10/11 | 不低于 4b |
| 留出集单一循环 | 6/13（276,368 / 30） | —（D-072 三次：12、10、11） | 不低于 10/13 |

按数字，单一循环两组都没达到标准，团队持平。

**调参集。** 所有题第一次请求的提示令牌都正常（约 2 万）。
- `todo_html` 两种模式都以 `enter_key_and_trim` 失败，3b、4b 都失败过，属于这道题本身的波动。
- 单一循环 `fix_inventory` 以 `_guard`、`_reason` 失败（答案被静态检查拦下），3b、4b 都通过。没有保留答案（未加 `--keep-answers`），看不到具体写了什么。只此一次，判断不了是不是回退。

**留出集。** 这一组有 9 题处在服务商的“少开销”状态：第一次请求只有 1,457～1,845 或约 6,400 提示令牌，平时约 2 万（D-072、第 11 节）。同一次评测里，前两组的请求都正常，代码没变，67 项对照测试也保证请求和 4b 逐字相同，所以这是服务商一侧的状态，不是 4c 改的。
- 4 道写代码的题（`business_days`、`csv_line`、`expr_eval`、`fix_split_amount`）都以 `_load` 失败（交上来的代码载入不了），而且都处在少开销状态；开销正常的 2 道代码题（`fix_topo_sort`、`fix_word_wrap`）都通过。`fix_split_amount` 在 1109 和 1109b 两次评测里也处在少开销状态（1,494、1,901），也都失败；开销正常的那次（`bench-holdout-loop-todo.json`，20,184）通过。前 3 道题以往开销正常时 3 次都通过。
- `side_effects_qa` 的 `mutates_arguments` 以往 3 次都失败，这次也失败，和状态无关。
- `edge_eval_qa`（`window_9_10`）、`failing_tests_qa`（`failing_tests`、`passed_count`）这次处在约 6,400 的状态，以往开销正常时都通过。第 11.1 节已经说明，问答题的失败和开销缺失没有确定的因果关系，这里只记下来。

结论：调参集看不出 4c 的回退（团队持平；单一循环少的两题，一题是老问题，一题只出现一次）。留出集 6/13 低于标准，但 7 道失败里有 6 道处在服务商的少开销状态，另一道以往一直失败；代码题的失败和这个状态高度相关（4 次评测里，代码题处在少开销状态共 6 次，全部失败；开销正常共 18 次，全部通过），仍属相关，没有证明因果。按 D-035 不据此调提示。要拿可比的数字，可以在服务商状态正常时重跑留出集；用户 2026-11-11 决定今天先停在 4c，是否重跑另定。

### 4c 留出集复跑（2026-11-12，用户“重跑下留出集吧”“那在云端跑吧”）

在云端会话里跑，代码是 `stage4c` 分支的 `25aa47c`，设置与用户本机 `.env` 相同（中转站上的 `gpt-5.6-luna`，`AGENT_MODEL_CONTEXT_TOKENS=128000`，模型和单步时限 90/240 秒，`RAG_JSON_MODE=true`，`RAG_ENABLE_THINKING=false`）。留出集单一循环，每题 1 次，只交答案，`--max-runs 13`。输出 `output/bench-holdout-loop-4c-rerun.json`、`answers-holdout-4c-rerun/`、`traces-holdout-4c-rerun/`（只在那次云端会话里，没有拷回本机）。

结果 **12/13**，达到不低于 10/13 的标准。
- 唯一失败的是 `side_effects_qa`（5 项过 4 项，缺 `mutates_arguments`），以往 4 次都在这一项失败，和服务商状态无关。
- 有 3 题第一次请求处在约 6,400 的少开销状态（`call_chain_qa` 6,664、`role_perms_qa` 6,431、`side_effects_qa` 6,467），都是问答题；其余 10 题约 2 万，全部通过，包括上次失败的 4 道代码题。
- 代码题和少开销状态的相关性维持不变：这次代码题都处在正常状态，全部通过（累计正常 24 次全部通过，少开销 6 次全部失败）。

结论：4c 留出集通过，可以并入 `main`（用户 2026-11-12“可以”，D-077）。
