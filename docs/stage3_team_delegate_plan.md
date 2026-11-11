# 架构第 3 阶段：团队改为单一循环里的委派工具（修改方案）

日期：2026-11-10。状态：**方案，待用户确认第 9 节后实现**（stage3-plan-001）。实现编号 `stage3-team-loop-001`（3a）和 `stage3-cleanup-001`（3b）。
来源：用户 2026-11-10“先进行架构第三阶段方案确定吧”。依据是 [架构梳理](architecture_review.md) 第 5、6 节和当前代码（`0f92d5c`）。

## 1. 目标与边界

[架构梳理](architecture_review.md) 第 6 节第 3 阶段原文：

> 团队模式改为 `delegate` 工具，验收门槛改为拦截型 hook；删除执行器注册表、路由和 `team_v2.py` 的大部分。判断标准：团队评测不低于现在。

本方案把它落到文件和步骤上，并分成两步：

- **3a 新实现与旧实现并存**：团队模式改由单一循环（`AgentLoopRunner`）担任负责人，`delegate` 是循环里的宿主工具；旧的 `DelegatingTeamRunner` 先保留，用一个临时开关切换，两种实现各跑一次评测对比。
- **3b 删除**：3a 的评测不差于旧实现后，再删 `team_v2.py` 的大部分、执行器注册表和路由，运行时直接接收一个执行器。

**不在本阶段**（留给第 4 阶段）：规划模式（`Planner` + `ToolCallingStepRunner`）、`TaskState` 精简、服务层拆分、模型档案。规划模式这次只是不再经过注册表和路由，行为不变。

## 2. 现状（`0f92d5c`）

| 部分 | 文件 | 行数 | 现在怎么工作 |
|---|---|---|---|
| 团队 | `app/agent/team_v2.py` | 704 | 负责人是一个自写的工具调用循环，只有 `delegate`（可选 `review`、`verify`）；每个成员是一个新的 `ToolCallingStepRunner`，最后回 JSON 信封；宿主在“成员成功 + 最后一次写入后有通过的运行”时直接结束任务（D-019） |
| 团队入口 | `team_v2.SingleStepPlanner` + `RagAgentService._team_registry` + `_team_requirements` | — | 用固定一步计划进入运行时，再靠能力 `agent_team` 路由到团队执行器；步骤时限要比团队时限多 5 秒 |
| 单一循环 | `app/agent/agent_loop.py` | 375 | `LoopPlanner`（继承 `SingleStepPlanner`）+ 能力 `agent_loop` 路由到 `AgentLoopRunner`；纯文本即完成，`todo`、`ask_user` 是控制工具 |
| 注册表与路由 | `runner_registry.py`、`runner_router.py`、`routing_step_runner.py` | 287 | 只剩三个执行器（`tool_calling`、`agent_loop`、`agent_team`），每个任务实际只用其中一个 |
| 模式选择 | `route_selection.py` | 127 | 选 single / team（auto 模式），并决定是否只读；**保留不动** |

团队的已有评测都很旧（2026-11-04，换中转站模型和加 hooks 之前），只覆盖 `parse_duration` 等少数几题，严格通过 1/3～2/3。**没有可直接比较的当前基线**，所以第 4 节第一步是先跑一次基线。

## 3. 目标结构

```
团队模式 = AgentLoopRunner（负责人）
   工具：delegate（必有）、review（可选）、todo
   delegate(task, tools) → 宿主起一个成员子循环：
        AgentLoopRunner（新上下文、只给授权的工具、无 ask_user）
        → 纯文本报告 + 宿主观察到的证据（写了哪些文件、最后一次运行是否通过）
   完成：
     · 成员成功且完成条件满足 → 宿主直接结束，答案取该成员的报告（保持 D-019）
     · 负责人回纯文本 → 先过“完成前”拦截（PassingRunGate），不满足就提示一次，再不满足则失败
```

### 3.1 委派工具（新文件 `app/agent/delegation.py`）

- `DelegationOptions` 从 `team_v2.py` 搬过来，字段含义不变；`max_lead_turns` 改为负责人循环的 `max_tool_rounds`。
- `delegate(task, tools)`、`review(focus)` 的参数、授权检查（`_grant`）、报告字段（`member`、`status`、`result`、`files_written`、`last_run`、`passing_run_after_last_write`、`delegations_remaining`、`unavailable_tool`）**保持不变**，负责人看到的东西和现在一样。
- 成员改用 `AgentLoopRunner`：
  - 只给授权的工具，去掉 `ask_user`（成员不能向用户提问），`todo` 保留；
  - 轮数和调用次数用现在的 `worker_tool_rounds=12`、`worker_tool_calls=24`；
  - 成员的任务说明（`_worker_brief`）原样沿用，只删掉“最后回 JSON”的要求；
  - 成员回纯文本即为 `succeeded`；抛出 `StepRunError` 时按现在的方式记为 `error`，宿主保留已观察到的工具结果。
- 审查员（`review=True` 时）需要“通过/退回”的明确结论：给审查员一个控制工具 `review_decision(decision, problems)`；审查员没调用它就结束，按“退回”处理。
- 成员的所有调用仍包在 `trace_scope('member', member=...)` 里，追踪结构不变（D-073）。

### 3.2 循环需要的两处扩展（`app/agent/agent_loop.py`）

1. **宿主工具**：`AgentLoopRunner` 接收一组宿主工具（`delegate`、`review` 由团队装配传入），和 `todo`、`ask_user` 一样由循环自己处理，不进工具注册表。宿主工具可以返回“结束任务”（带答案），循环据此直接返回，用来实现 D-019 的“宿主判定完成”。
2. **完成前拦截**：负责人回纯文本时，循环先发一个新的拦截事件 `before_finish`（见 3.3）。被拒时把拒绝理由作为 HOST 消息加入，最多一次；第二次仍被拒则以 `TeamError('team acceptance gate not met')` 失败，和现在一致。

普通单一循环不传宿主工具、也没有注册 `before_finish` 拦截，行为不变；这一点要有测试守住。

### 3.3 验收门槛改为拦截型 hook（`app/agent/hooks.py`、`builtin_hooks.py`）

- `GATE_EVENTS` 增加 `before_finish`。事件只带宿主值：`task_id`、本次运行的写入次数、最后一次写入后是否有通过的运行、审查是否在最后一次写入后通过；不带模型文本。
- 新增 `PassingRunGate`：`require_passing_run` 时，**本次运行有过成功写入**才拒绝“最后一次写入后没有通过的运行”的结束，没有写入就不拦（基线发现旧实现在问答题上误拦，见 10.1）；`review` 时还要求审查在最后一次写入后通过。宿主在委派后直接结束任务（3.2 第 1 项）只在“有过写入且条件满足”时发生；没有写入时由负责人自己回答，因为成员的报告不一定是目标要求的答案形式。它只在团队装配时注册。
- 拦截的现有规则不变：出错或超时（5 秒）算拒绝；只能加严。
- **兼容影响**：这是 hook 接口的新增事件，已有观察者会多收到一种事件（`AuditHook`、`TraceFileHook` 按类型记录，不受影响；需要测试确认追踪树仍正确）。

### 3.4 运行时与服务层（3b）

- `AgentRuntime` 不再接收 `runner_registry`、`routing_policy`、`runner_requirements`，改为接收一个 `runner_factory(deps) -> StepRunner`。`RoutingStepRunner` 现在负责的三件事要搬走：
  - 依赖重绑（`invalidate`，`rebind` 时用）→ 运行时自己在 `rebind` 时重新调用工厂；
  - 格式修复计数（`format_repair_count`）→ 直接读执行器的同名属性；
  - 路由事件（`decisions`，进入运行结果和追踪）→ 不再产生；`route` 事件里已有 `mode` 和 `runner`，追踪和网页改读它。
- `RagAgentService.__init__` 删除 `runner_registry`、`routing_policy`、`runner_requirements` 三个参数（**公开接口的破坏性改动**，见第 7 节）。
- `SingleStepPlanner` 改名并移到 `agent_loop.py`（`LoopPlanner` 已在那里），团队和循环共用。
- 删除：`runner_registry.py`、`runner_router.py`、`routing_step_runner.py`；`team_v2.py` 删到只剩被别处导入的常量，最终删除整个文件，常量（`READ_TOOLS`）移到 `delegation.py`。
- 第 2 节所说的步骤时限“团队时限 + 5 秒”、`max_replans=0` 的装配逻辑合并成一处：整个任务只有一步时，步骤时限取执行器自己的时限加 5 秒。

### 3.5 独立验收测试（`--team-acceptance`）

现在是默认关闭的可选项（D-020～D-022），约 200 行（`verify` 工具、验证员、宿主跑验收测试、写入门）。真实评测里验证员常写出错误的测试（D-022），2026-11-04 之后没人用过。**建议本阶段删除**，第 9 节第 2 问请用户决定；如保留，按 3.1 的方式把验证员也改成子循环，工作量约多一半。

## 4. 步骤

| 步 | 内容 | 谁做 | 需要授权 |
|---|---|---|---|
| 0 | 团队基线：当前代码跑调参集团队模式各 1 次（`--modes team --write --sandbox`，需要 Docker 沙盒镜像），记录严格通过数、调用次数、令牌、用时 | Claude 运行，结果写进本文第 10 节 | **是**：真实模型调用 |
| 3a | 按 3.1～3.3 实现，加临时开关 `--team-impl v2|loop`（命令行、评测；默认 `v2`）；补测试 | Codex（按约定），Claude 审查 | 否 |
| 3a 评测 | 新实现跑同样的调参集团队模式 1 次；单一循环调参集跑 1 次，确认没被扩展影响 | Claude 运行 | **是** |
| 判断 | 团队严格通过数不低于第 0 步；调用次数和用时同时列出。不满足就停在 3a，修或回退，不进 3b | 用户决定 | — |
| 3b | 按 3.4、3.5 删除旧实现和临时开关，团队默认用新实现 | Codex，Claude 审查 | 否 |
| 3b 评测 | 团队、单一循环调参集各 1 次，确认 3b 只是删除、行为不变 | Claude 运行 | **是** |

每一步单独提交（经用户同意），出问题可以只回退那一步。

## 5. 测试

- **新增 `tests/test_delegation.py`**：把 `tests/test_team_v2.py` 里仍然成立的用例迁过来，覆盖：授权检查与越权拒绝、委派次数上限、成员失败时宿主保留观察结果、`unavailable_tool` 报告、完成条件满足时宿主直接结束、负责人提前回答被 `PassingRunGate` 拒一次后再失败、审查员不调 `review_decision` 按退回、成员没有 `ask_user`、成员调用都在 `member` 区间里。
- **循环**：`tests/test_agent_loop.py` 增加“不传宿主工具时行为不变”“宿主工具返回结束”“`before_finish` 被拒一次后提示、两次后失败”。
- **hooks**：`before_finish` 是拦截事件、出错或超时算拒绝、观察者能收到、追踪树正确。
- **3b 删除**：`tests/agent/test_runner_registry.py`（5 项）、`test_runner_routing.py`（7 项）、`test_runtime_routing.py`（9 项）随模块删除；`test_team_v2.py`（43 项）迁完后删除；`test_run_options.py`、`test_web_loop_runner.py`、`test_trace.py` 里用到注册表参数的地方改为新的装配方式。删掉的每一项要在记录里说明是“随模块删除”还是“已迁到哪里”。
- **防回退守卫**：仿照 `tests/test_stage0_cleanup.py`，3b 后断言三个路由模块和 `team_v2.py` 不再存在、`app/` 里没有对它们的导入。
- 每步都要 `init.ps1` 通过。

## 6. 记录

- `docs/DECISIONS.md` 新增一条（D-075）：团队改为委派工具、完成条件改为 `before_finish` 拦截、删除注册表与路由、是否删除独立验收测试。
- `AGENTS.md` 的两条全局不变量要改写（第 7 节），改前需用户同意。
- `docs/architecture_review.md` 第 6 节加第 3 阶段结果；`docs/agent_runtime.md` 中路由和团队的描述同步。
- `feature_list.json`、`progress.md`、`session-handoff.md` 按 AGENTS.md 收尾。

## 7. 兼容影响（AGENTS.md 要求改接口前说明）

- **AGENTS.md 不变量**：
  - “ToolCallingStepRunner 与 AgentTeamRunner 平级独立；Team 使用独立 Lead、确定性 Coordinator 和轻量 Session，不嵌套 Runtime” → 改为“团队是单一循环里的 `delegate` 工具；成员是独立上下文的子循环，不嵌套 Runtime；完成由宿主判定”。
  - “Runner 必需能力来自独立可信来源，不由 RouteDecision 自我授权” → 改为“执行方式由宿主选择（`route_selection` 的宿主规则在前，模型只能在规则允许的范围内选），模型不能放宽”。
- **公开接口**：`RagAgentService(runner_registry=..., routing_policy=..., runner_requirements=...)`、`AgentRuntime` 的同名参数、`register_agent_loop_runner`、`register_delegating_team_runner` 删除。仓库内只有测试在用；外部调用方（如果有）需要改。
- **运行结果**：路由事件（`decisions`）不再出现在运行结果和审计里；审计文件版本号不变，追踪查看器对缺少它的记录要能正常显示（旧文件里有它也要能读）。
- **行为**：成员不再回 JSON 信封；团队的提示文字会有改动。这些只能靠第 4 节的评测判断好坏。

## 8. 风险

- **评测样本小**：团队每题只跑 1 次，通过数差 1 不一定说明问题；同时看调用次数、用时和失败类型。
- **成员没了 JSON 状态**：以前成员可以明确报 `failed`；改成纯文本后，失败只能从文本和宿主证据看出。完成判定本来就靠宿主证据（通过的运行），不靠成员自述，所以影响有限，但负责人读报告的方式会变。
- **测试改动大**：约 64 项测试删除或迁移（第 5 节），审查时要逐项核对没有丢掉仍然成立的约束。
- **沙盒依赖**：第 0 步和评测都需要 Docker 和 `aletheia-sandbox:py313` 镜像。

## 9. 需要用户确认的问题

1. **先跑团队基线**（第 4 节第 0 步）：调参集团队模式、写文件加沙盒各 1 次，可以吗？还要不要加跑留出集？（建议只跑调参集，留出集等 3a 结果接近时再跑。）
2. **独立验收测试**（`--team-acceptance`，第 3.5 节）：建议删除；还是保留并改成子循环？
3. **负责人能不能自己用只读工具**：建议 3a 保持现状（负责人只能委派），这样新旧实现可以直接比较；以后想放开再单独评测。
4. **分 3a、3b 两步、各自评测和提交**，可以吗？
5. **由谁实现**：按约定 Codex 有额度就交给它，Claude 审查；没有额度由 Claude 来做。

**用户回答（2026-11-10）**：“都按建议”。即：只跑调参集的团队基线（写文件加沙盒），留出集等 3a 结果接近时再定；删除独立验收测试；3a 负责人只能委派；3a、3b 分开评测和提交；Codex 实现、Claude 审查。

## 9.1 3a 实现记录（2026-11-10）

- Codex 实现，Claude 审查。新文件 `app/agent/delegation.py`（`DelegatingLoopRunner` 继承 `DelegatingTeamRunner`，沿用授权、报告和选项，只换负责人和成员循环）；`agent_loop.py` 增加 `HostTool`/`HostToolResult`、`allow_ask_user`、`instructions`、`finish_state`、`before_finish`；`hooks.py` 增加 `BEFORE_FINISH`；`builtin_hooks.py` 增加 `PassingRunGate`；服务、命令行、评测加 `team_impl`/`--team-impl`（默认 `v2`）。
- 与方案的差别：新实现不再写旧的 `Team trace` 日志行，评测行里没有 `team_trace`、`team_model_calls`，对比用 `--keep-traces` 的追踪文件和行里的 `model_calls`；负责人不能 `ask_user`，缺信息时在答案里说明（旧实现可回 `blocked`）。
- 审查修改：不能提问时的提示原写“把缺的信息报告给负责人”，对负责人不成立，改为“在最终答案里说明缺什么”，补 1 项测试。
- 验证：新增测试 45 项（`tests/test_delegation.py`、`tests/test_agent_loop.py`、`tests/test_hooks.py`）；init.ps1 1851 项通过、16 项跳过。没有真实模型运行。

## 10. 评测记录

### 10.1 第 0 步：团队基线（2026-11-10，`0f92d5c`，旧实现 `DelegatingTeamRunner`）

命令：`.venv/Scripts/python.exe -m bench.run_bench --confirm-live --tasks all --modes team --runs 1 --write --sandbox --max-runs 11 --keep-traces output/traces-team-baseline --out output/bench-tune-team-baseline.json`（团队时限默认 180 秒）。

| 题 | 结果 | 模型调用 | 令牌 | 用时（秒） | 说明 |
|---|---|---|---|---|---|
| `billing_qa` | 失败 | 6 | 130,363 | 44.0 | 负责人报失败（见下） |
| `config_merge_qa` | 通过 | 9 | 36,934 | 105.7 | 第二个成员跑了一次 `run_python`，满足了完成条件 |
| `fix_inventory` | 通过 | 10 | 233,006 | 82.1 | |
| `fix_lru_cache` | 通过 | 9 | 210,491 | 73.5 | |
| `fix_rate_limiter` | 通过 | 9 | 209,059 | 79.2 | |
| `log_errors_qa` | 失败 | 10 | 218,170 | 54.3 | 负责人报失败（见下） |
| `merge_intervals` | 通过 | 7 | 163,300 | 50.7 | |
| `parse_duration` | 通过 | 8 | 180,386 | 66.0 | |
| `sales_csv_qa` | 失败 | 11 | 241,924 | 88.6 | 负责人报失败（见下） |
| `semver_compare` | 通过 | 8 | 185,830 | 62.5 | |
| `todo_html` | 失败 | 8 | 193,454 | 99.3 | 10/11，错在纯文本匹配 `enter_key_and_trim`（单一循环开沙盒时也错在这一项） |
| **合计** | **7/11** | **95** | **2,002,917** | **806** | |

对比：单一循环开沙盒（loop-sandbox-check-001，2026-11-07）10/11，48 次调用，1,052,636 令牌，333 秒。

**三道问答题失败的原因**（由团队轨迹确认：都是 1～2 次委派、0 次写入、负责人 3～4 轮）：开沙盒时 `require_passing_run=True`，完成条件是“最后一次写入后有一次通过的运行”。问答题不写文件，`has_passing_run_after_last_write` 在没有写入时也要求有一次通过的运行；成员只读文件没有运行，负责人给出答案时被宿主拒绝，第二次就回了 `failed`。`config_merge_qa` 能通过，只是因为第二个成员碰巧跑了一次 `run_python`。这是旧实现的缺陷，不是模型能力问题。

**对 3a 的影响**：新实现的 `PassingRunGate` 改为**只在本次运行有过成功写入时**才要求通过的运行（没有写入就不拦）。这会让问答题的结果和旧实现不可直接比较，所以 3a 评测分两部分看：代码类 7 题（实现、修复、前端）和旧实现逐题比较；问答类 4 题单独列出，记为“完成条件修正后的结果”。判断标准相应改为：**代码类 7 题严格通过数不低于基线的 6/7，总数不低于 7/11**。

### 10.2 3a 评测（2026-11-10，`0f92d5c` 加 3a 未提交改动）

命令：同 10.1，加 `--team-impl loop`，报告 `output/bench-tune-team-loop-3a.json`，追踪 `output/traces-team-loop-3a/`；单一循环 `--modes single`（答案交付），报告 `output/bench-tune-loop-3a.json`。

| 题 | 旧实现（基线） | 新实现 | 新实现调用 / 令牌 / 秒 | 单一循环 |
|---|---|---|---|---|
| `fix_inventory` | 通过 | 通过 | 9 / 202,741 / 74.2 | 通过 |
| `fix_lru_cache` | 通过 | 通过 | 8 / 184,078 / 58.9 | 通过 |
| `fix_rate_limiter` | 通过 | 通过 | 10 / 231,078 / 72.0 | 通过 |
| `merge_intervals` | 通过 | 通过 | 7 / 159,961 / 50.8 | 通过 |
| `parse_duration` | 通过 | 通过 | 8 / 176,935 / 61.8 | 通过 |
| `semver_compare` | 通过 | 通过 | 8 / 184,058 / 71.4 | 通过 |
| `todo_html` | 失败（`enter_key_and_trim`） | 失败（同一项） | 10 / 253,489 / 169.7 | 通过 |
| **代码类小计** | **6/7** | **6/7** | | 7/7 |
| `billing_qa` | 失败（完成条件误拦） | 通过 | 4 / 85,599 / 25.6 | 通过 |
| `config_merge_qa` | 通过 | 失败（`top_keys`） | 5 / 20,945 / 33.6 | 失败（`top_keys`） |
| `log_errors_qa` | 失败（完成条件误拦） | 通过 | 4 / 84,964 / 22.3 | 通过 |
| `sales_csv_qa` | 失败（完成条件误拦） | 通过 | 19 / 412,643 / 123.1 | 通过 |
| **问答类小计** | 1/4 | 3/4 | | 3/4 |
| **合计** | **7/11**，95 次，2,002,917 令牌，806 秒 | **9/11**，92 次，1,996,491 令牌，763 秒 | | **10/11**，26 次，520,667 令牌，199 秒 |

- **判断标准**（10.1）：代码类 6/7，不低于基线 6/7；总数 9/11，不低于 7/11。**达到**，可以进入 3b。问答类多出的 2 题来自完成条件的修正，不能算作新实现本身更好。
- 调用次数、令牌和用时与旧实现基本持平；`sales_csv_qa` 用了 19 次调用，是新实现里最多的一题。团队模式整体仍比单一循环贵约 4 倍、慢约 4 倍。
- **`config_merge_qa` 的 `top_keys`**：团队新实现和单一循环这次都错在同一项（顶层键名排序）。单一循环此前 8 份调参集报告里这题都通过。3a 没有改单一循环的提示和消息（不传宿主工具时行为不变，有测试守住），所以更可能是模型这次的波动；要确认，可以单独重跑这一题几次（未授权，未跑）。
