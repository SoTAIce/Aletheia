# 第 4 阶段：拆服务层、循环独立运行、删除规划—执行骨架（修改方案）

日期：2026-11-11。状态：**用户已确认第 7 节；4a 已提交 `e6a9856`；4b 已实现并评测（第 10、11 节），未提交**（stage4-plan-001；4a 为 stage4a-001）。
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

