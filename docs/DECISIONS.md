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
