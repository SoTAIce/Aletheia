# Aletheia 设计决策记录

格式按课程 L05：日期 / 决策 / 理由 / 被否方案 / 约束 / 来源。只收录现有报告中已写明的决策；原文没写到的写“未记录”，不补推测。
新决策在此追加。推翻旧决策时，新开一条并注明 supersedes，不改写旧条目。

## D-001 2026-10-17 记忆职责分离

- 决策：ConversationMemory 管会话存储/视图；WorkingMemory 是任务真值；ContextManager 只生成模型可见副本。
- 理由：防止投影或摘要改写权威状态。
- 被否方案：另建统一 Memory 框架（PROJECT_HANDOVER §1 明确不要再造）。
- 约束：摘要不能覆盖当前指令或原文；`clear()` 不删除磁盘。
- 来源：[2026-10-17 memory-context 计划](superpowers/plans/2026-10-17-memory-context.md)，[PROJECT_HANDOVER §5](PROJECT_HANDOVER.md)

## D-002 2026-10-25 暂不实现 warm body cache

- 决策：保持 hot 近期 Turn + 元数据/索引 + cold 原文，不实现 warm tier。
- 理由：I/O 优化后只解码近期 20 条 + 一个摘要批次，warm cache 要解决的剩余成本已经很小；它也不解决 O(H) 元数据审计和检索质量问题。
- 被否方案：直接实现有界 LRU warm cache。只有离线 trace 达到预设门槛（≥50% 命中、P95 读延迟降低 ≥20%、append 回退不超过 10%）才重新评估。
- 约束：门槛是提案值，不是实测值。
- 来源：[conversation_memory_tiers.md](conversation_memory_tiers.md)

## D-003 2026-10-31 Runner 路由是确定性的，所需能力来自可信来源

- 决策：RunnerRequirements 只来自单独注入的应用回调；RoutingPolicy 同步、确定性；RouteDecision 不含权威能力字段。
- 理由：模型自称的能力或 Runner 名不可信，不能自我授权。
- 被否方案：异步/基于模型的路由，Planner 能力提示（推迟）。异步 policy 会被拒绝，以免引入不受控的模型等待。
- 约束：没有候选或存在歧义时抛 StepRoutingError，不静默回退。
- 来源：[multi_runner_infrastructure.md](multi_runner_infrastructure.md)

## D-004 2026-11-02 Team 是平级 Runner，不嵌套 Runtime

- 决策：AgentTeamRunner 与 ToolCallingStepRunner 平级注册；独立 Lead + 确定性 Coordinator + 轻量 Session；顺序调度。
- 理由：复用现有 Registry/RoutingPolicy 扩展点，StepExecutor 提交门仍留在外层。
- 被否方案：嵌套 AgentRuntime、递归团队、复杂并行、跨进程检查点（均明确不支持）。
- 约束：宿主契约高于 Lead；默认最多 4 成员等预算上限。
- 来源：[agent_team_v1.md](agent_team_v1.md)，[team_contract_review_poc.md](team_contract_review_poc.md)

## D-005 2026-11-03 普通模式仍为默认，Team 需显式 opt-in

- 决策：保持 `--execution-mode single` 为默认。
- 理由：三组真实任务严格交付：普通 2/3，Team 0/3；没有证据表明 Team 质量更好。
- 被否方案：默认启用 Team，或宣称 Team 更可靠。
- 约束：不能用中间稿得分替代最终严格交付。
- 来源：[agent_team_task_comparison_2026-11-03.md](agent_team_task_comparison_2026-11-03.md)

## D-006 2026-10-21 长期记忆：PostgreSQL 为权威，Milvus 是可重建索引

- 决策：先写数据库，再更新索引；MemoryIndexSyncError 保留并上报，不自动重放 `remember()`。
- 理由：跨库没有事务；盲目重放会重复写入。
- 被否方案：分布式事务、outbox/持久重试（推迟），以及未经校准的置信度阈值。
- 约束：用户隔离、乐观版本校验；Retriever 回查权威行。
- 来源：[2026-10-21 longterm 计划](superpowers/plans/2026-10-21-longterm-agent-integration.md)

## D-007 2026-11-04 按用户要求删除 tests/ 与验证脚本

- 决策：删除 tests/、scripts/ 验证脚本和临时产物，外部 TEMP 备份留存。
- 理由：用户要求精简仓库。
- 被否方案：未记录。
- 约束：此后行为修改必须先恢复对应测试或经授权补充；历史 1093/2 不能当作当前回归结果。
- 来源：[project_cleanup.md](project_cleanup.md)

## D-008 2026-11-04 Harness 入口用 PowerShell，且只读

- 决策：用 `init.ps1` 代替课程的 `init.sh`；只校验、只打印启动命令，不安装依赖、不启动服务。
- 理由：项目在 Windows 上开发；AGENTS 禁止自动安装或修改配置。
- 被否方案：照搬 `init.sh` 的 install → start 流程；为提高结构评分另建冗余入口。
- 约束：init 通过只代表 static/import 正常、当前 tests/ 通过，不代表完整行为正确。

## D-009 2026-11-04 未授权提交时，用“已记录基线”替代 git checkpoint（被 D-010 部分取代）

- 决策：基线 = 已提交 HEAD + 工作区差异逐文件 SHA-256 清单 + 只读核对命令。
- 理由：课程要求有可复原的 checkpoint；AGENTS.md 禁止未经授权提交。
- 被否方案：自动 commit（违反授权边界）；忽略这项要求。
- 约束：清单不能防止工作区丢失；用户授权后应转为真正的 commit。
- 来源：[baseline.md](harness/baseline.md)
- 来源：[Harness 手册](harness/README.md)，[合规审查](harness/conformance-audit-2026-11-04.md)

## D-010 2026-11-04 提交 harness 文件、新增最小测试；用户差异暂不提交

- 决策（用户选择）：本地提交 harness 管理文件、docs 和 `tests/test_harness_smoke.py`，不推送；长期记忆源码、README、旧 tests/scripts 删除项留在工作区，记入基线清单。
- 理由：满足课程 “Everything committed / All tests pass”，同时不把未验收的源码混进提交（PROJECT_HANDOVER §2）。
- 被否方案：三批全部提交（源码尚未验收）；不提交、只保留清单。
- 约束：init.ps1 要求 tests/ 存在并通过；后续行为改动要补充对应测试。
- 来源：本会话用户回答；[baseline.md](harness/baseline.md)

## D-011 2026-11-04 历史测试从 git 取到 TEMP 运行，不恢复进仓库

- 决策：需要回归证据时，用 `git archive -o <TEMP>/src.tar 9138ac1 tests/agent` 解压到 TEMP 新目录运行，测完立即删除（长路径用 `\?\` 前缀）；需要长期保留的结论写成 tests/ 下的小型契约测试。
- 理由：用户精简了 tests/；已跟踪的旧测试仍在 git 历史中，不需要依赖不可访问的 TEMP 备份。
- 被否方案：把旧 tests/ 整体恢复进仓库（违背用户的精简决定）；只做文档核对而不跑测试。
- 约束：PowerShell 管道会破坏二进制数据，必须用 `-o` 输出文件；运行时用仓库 pyproject 的 pytest 配置（`-c pyproject.toml`）。
- 注意：部分测试会启动 filesystem MCP 子进程，MCP stdio 客户端不会把 `PYTHONDONTWRITEBYTECODE` 传给子进程，所以会在 `app/tools/` 下生成 `__pycache__`。测完要删除（`find app -name __pycache__`）。
- 来源：[Team 一致性审查](harness/team-audit-2026-11-04.md)

## D-012 2026-11-04 工具参数解析失败可有界修复；失败信息带异常类型链

- 决策：适配器把无法解析的 tool call 参数抛为 `ModelToolArgumentsError`（`ModelResponseError` 的子类，消息文本不变），只带 reason、经过过滤的工具名和参数长度。Team Session 遇到它时追加宿主修复指令后重试，和截断修复共用 `max_format_repairs` 预算；single runner 单独计数（`argument_repair_count`），上限同样是 `max_format_repairs`。被拒绝的那一轮不执行任何工具。`StepModelError` 的消息附带异常类型链（只有类型名）。
- 理由：frontend-compare-001 中 team 因为一次参数 JSON 错误整体失败，模型既看不到错误也无法修正；single 的失败原因无法定位。
- 被否方案：在适配器内部自动重发请求（会让 Team 的模型调用预算少算）；把原始参数回显给模型（会泄露、放大模型原文）。
- 约束：修复次数耗尽仍然失败；其他 `ModelResponseError` 不做修复。
- 来源：[阶段 1 记录](harness/capability-phase1-2026-11-04.md)

## D-013 2026-11-04 用固定评测集衡量交付能力

- 决策：新增 `bench/`：3 个固定任务（前端页面、函数实现、读代码修 bug）、隐藏 checker、确定性评分器和运行器。真实运行必须加 `--confirm-live`，并受 `--max-runs` 上限约束。
- 理由：课程要求用可复现的验证判断完成度；单次演示不能说明能力有没有提升。
- 被否方案：继续用一次性人工对照；让模型自己评分。
- 约束：候选 Python 代码先过 AST 守卫，再在 `python -I` 子进程中带超时执行——只防误伤，不是安全沙箱；报告不含模型原文。每轮真实运行都要用户单独授权。
- 来源：[bench/README.md](../bench/README.md)

## D-014 2026-11-04 沙盒执行采用 Docker，第一版只支持 Python

- 决策（用户选择）：采用 [沙盒执行设计方案](sandbox_execution_design.md) 的 Docker 方案。第一版只支持 Python；接受默认限制：单次 30 秒、512MB 内存、1 个处理器、128 个进程、每步最多运行 10 次。授权 Claude 在 Docker 引擎由用户启动后拉取 `python:3.13-slim` 并构建本地沙盒镜像。
- 理由：本机已安装 Docker Desktop，隔离参数现成；先用最小范围验证执行验证闭环是否有效。
- 被否方案：WSL 低权限用户（默认能访问 Windows 磁盘）；本机子进程（隔离太弱）；Windows 沙盒（未启用、难以自动化）；第一版就支持前端无头浏览器（镜像大、工作量大）。
- 约束：Docker 引擎由用户启动，Claude 不启动或修改系统服务；只开放 `run_tests` / `run_python`，不开放任意命令；写入和运行工具只授予执行成员；实施按设计第 11 节分步进行，每步单独登记功能条目。
- 来源：本会话用户回答

## D-015 2026-11-04 受限写入：只在配置写根时开放 write_file，审查成员不得获得写入/运行工具

- 决策：团队模式默认白名单保持只读；只有命令行传入 `--write-root`（或配置了写根）时，才把 `write_file` 加入白名单（`app/agent/__main__.py` 的 `team_options_for`）。`WORKER_ONLY_TOOLS = {write_file, run_tests, run_python}` 只能授予执行成员；负责人给审查成员申请这些工具时，和越过白名单一样按权限错误处理，直接让团队失败。评测集新增 `--write` 模式：临时根目录同时作为读根和写根，智能体把交付物写入指定文件，评分读取该文件（不跟随链接，大小有上限）；文件没写或没改动都算失败。
- 理由：沙盒设计第 5 节；交付文件代替“把代码塞进答案”，同时绕开第一轮暴露的交付格式问题。
- 被否方案：把越权请求改成可恢复的拒绝（会改变历史测试 `test_team_cannot_grant_tools_outside_host_allowlist` 固定的行为，超出本步范围）；由宿主新建独立工作目录的命令行选项（评测运行器已经负责创建临时根目录，命令行暂不增加）。
- 约束：本步不调用真实模型；运行工具和 Docker 属于第二、三步。
- 来源：[沙盒执行设计方案](sandbox_execution_design.md) 第 5 节

## D-016 2026-11-04 沙盒执行器：固定镜像、固定限制、只允许运行工作目录内的 Python 文件或 pytest

- 决策：镜像 `aletheia-sandbox:py313` 由 `deploy/sandbox/Dockerfile` 构建（`python:3.13-slim` + `pytest==8.3.4` + uid 1000 用户）。执行器 `app/tools/sandbox/executor.py` 每次运行都起一个新容器，参数由宿主写死：断网、根文件系统只读、`/tmp` 64MB 且不可执行、uid 1000、去掉全部特权、512MB 内存（不允许交换）、1 个处理器、128 个进程、30 秒、只传两个固定环境变量、只挂载本任务工作目录、输出读取时截断到 20,000 字符。超时后强制删除容器。请求在启动 Docker 之前先校验：拒绝绝对路径、盘符、`..`、链接、不存在的文件、非 `.py` 文件，以及带特殊字符的参数。所有容器带标签 `aletheia.sandbox=1`，清理只针对这个标签，不影响用户的其他容器。
- 理由：设计第 6、8 节；用户选择 Docker 和默认限制（D-014）。
- 被否方案：开放任意命令；把限制写进镜像（容易被绕过，也无法测试）；容器内用 `head` 截断输出（会丢失退出码）。
- 约束：真实容器测试需要显式开启（`ALETHEIA_DOCKER_TESTS=1`），init 不依赖 Docker。本步还没有把运行工具交给模型（第三步）。
- 来源：[沙盒执行设计方案](sandbox_execution_design.md)

## D-017 2026-11-04 运行工具通过独立的沙盒 MCP 服务接入；团队结束任务前必须有通过的运行

- 决策：新增 `app/tools/sandbox/server.py`，只提供 `run_tests` 和 `run_python` 两个工具，工作目录在启动时由宿主固定为写根。命令行 `--sandbox` 需要同时提供 `--write-root`，并且在调用模型之前先检查镜像是否可用，否则直接退出（退出码 2）。开启后，普通模式和团队模式都通过现有的 MCP 加载路径获得这两个工具。团队模式下，这两个工具只能授予执行成员（D-015），并新增 `TeamOptions.require_passing_run`：负责人要求结束任务时，宿主检查执行成员在最后一次成功的 `write_file` 之后，是否有一次退出码为 0 的运行；如果没有，这条结束命令被拒绝，但团队可以继续（属于可恢复的门槛）。新增 `--team-timeout`，默认值不变。
- 理由：设计第 6.4 节要求审查依据运行结果，用宿主门槛代替提示词约束。用 MCP 子进程接入，避免改动用户尚未提交的 `rag_agent_service.py`；子进程只继承 MCP 的最小环境变量，不含任何密钥。
- 被否方案：改 `rag_agent_service.py` 注入本地工具（会和用户未提交的改动混在一起）；只靠提示词要求审查成员先看运行结果。
- 约束：普通模式没有审查环节，只提供工具，不设门槛；普通模式每步只有 4 轮、8 次工具调用（`RunnerLimits` 默认值，本步没改），写→跑→改的循环可能会被截断，第四步评测时观察。
- 来源：[沙盒执行设计方案](sandbox_execution_design.md) 第 6 节

## D-018 2026-11-04 成员的协议失败交回负责人；格式修复次数按每次运行重置

- 决策：执行或审查成员的会话如果只因成员自身的协议错误失败（`SessionError`、`ModelToolArgumentsError`、`ContractError`，异常组里的每个叶子都必须属于这几类），团队不再结束，而是给负责人发一个 `member_failed` 事件（只含成员名、异常类型名和剩余次数）。失败的成员不能再运行；负责人可以新建替补成员，或者结束任务。新增 `TeamOptions.max_member_failures`（默认 2），超过即视为团队失败。格式修复次数改为在每次成员运行、每条负责人命令开始时重置，团队总数由新增的 `TeamOptions.max_format_repairs`（默认 8）限制。
- 理由：[团队模式失败诊断](harness/team-diagnosis-2026-11-04.md)显示，两次失败都是一个成员用完修复次数后整个团队立刻终止；普通模式每步都重置修复次数，并且失败后还能重新规划。
- 被否方案：自动重跑失败的成员（违背“不重试模型、工具和副作用”）；只调大修复次数（不能解决一次失败就终结团队的问题）。
- 约束：服务方错误、输出长度超限、团队预算、权限、工具副作用、过期和取消仍然直接结束团队（历史测试 9138ac1 锁定了其中几项）。替补成员占用 `max_members`（默认 4）的名额。
- 来源：用户 2026-11-04“先做第 1、2 项”

## D-019 2026-11-04 团队模式 V2：编排者—执行者，执行者即工具

- 决策：新增团队模式 V2（`app/agent/team_v2.py`）。负责人是一个普通的工具调用循环，只通过 `delegate(task, tools)`（以及可选的 `review(focus)`）委派；每次委派都用全新上下文运行普通模式的 `ToolCallingStepRunner`，工具来自父级工具注册表，按授予的名称过滤。委派结果由宿主整理成精简报告（状态、结果摘要、写过的文件、最后一次运行的退出码、是否满足结束条件）。是否完成由宿主判定：开启 `require_passing_run` 时，委派成功且最后一次写文件之后有通过的运行，团队直接完成，不再需要负责人发结束命令。模型审查可选，默认关闭。团队模式 V2 下外层计划只产生一步。V1 保留，通过 `--team-version 1|2` 选择，评测通过前默认仍为 1。
- 理由：[重新设计方案](harness/team-redesign-proposal-2026-11-04.md)第二节的 7 项结构缺陷（双层编排、长寿命会话、通用 JSON 命令、结束权交给模型、强制审查、自制协议、任务不匹配）；普通模式执行器同一模型下 2/2 通过。
- 被否方案：继续在 V1 上修补（6 轮修复每次都暴露新问题）；直接删除 V1（V2 未经真实评测）。
- 约束：工具白名单、审查者只读、委派次数与时间上限、失败不自动重放、轨迹只含宿主数据都保留。共享状态放在工作目录的文件里，不在消息历史里。
- 来源：用户 2026-11-04“提交基线后开工”
