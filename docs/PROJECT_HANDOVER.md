# Aletheia 项目交接：给 Claude / 下一位维护者

更新：2026-11-04。此文整合现有 docs 的架构、决策和验收结论，并核对当前源码入口。无需依赖之前的聊天记录；原始报告与计划均保留，见 [完整索引](DOCUMENT_INDEX.md)。

## 1. 接手时先知道这六件事

1. Aletheia 是 Python Agent/RAG 项目，主执行链为 Plan–Execute–Replan；单 Agent 和 Team 是平级 Runner。
2. 团队模式只剩 V2（2026-11-05 删除 V1，决策 D-025）：负责人委派给全新上下文的执行者，宿主判定完成。在 parse_duration、fix_inventory 上严格交付好于普通模式，但样本少，见第 7 节。
3. ConversationMemory、WorkingMemory、长期记忆和 ContextManager 各有职责；不要再建一套重复 Memory 框架。
4. 历史原文可恢复；运行中任务只能在同一服务实例暂停恢复，不能跨进程恢复执行。
5. 用户刚清理了 tests、验证脚本与临时产物。1093 passed/2 skipped 是清理前的历史回归；当前可直接运行的是 init.ps1（静态/入口 + `tests/` 下的确定性 pytest）；当前 `tests/` 已恢复历史测试（D-030），init.ps1 运行它们（2026-11-07 为 1594 passed、16 skipped；真实容器测试需设 `ALETHEIA_DOCKER_TESTS=1`，开启后 1231 passed）。
6. 当前工作区有用户未提交源码与删除项，不能把它当成干净的冻结提交，也不能擅自 reset、commit 或 push。

## 2. 仓库状态与启动

本次核对：分支 `main`，HEAD `9138ac1e7e5a5fbc4d31a7503bec325fc8a3f683`，本地 Agent Team V1 冻结提交；前置 Multi-Runner 冻结提交 `adea10e`。不据此推断远端已同步，远端状态本轮未核实。

本轮开始时已有源码修改：

- `app/agent/planner.py`
- `app/agent/step_executor.py`
- `app/agent/tool_calling_step_runner.py`
- `app/services/context_manager.py`
- `app/services/rag_agent_service.py`

此外已有 README/docs 修改、未跟踪设计文件及 scripts/tests 删除项。长期记忆注入与上下文投影改动留在工作区，冻结提交特意未包含它们。请用 `git diff` 判断具体改动，不能将这些差异一并提交为“本轮 Harness”。

Windows 根目录 `D:\tries\agent`；Python 3.11–3.13。依赖声明在 `pyproject.toml`；沿用现有 `.venv`，不自动重装。

```powershell
git status --short
git log -1 --oneline
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
```

`init.ps1` 只验证状态结构、源码语法和 CLI 导入，不连模型、Milvus/PostgreSQL 或 MCP。
配置由 `app/config.py` 从环境及 `.env` 读取。不要把配置类默认 `qwen-max`、历史报告 `qwen3.8-flash` 和本地当前有效配置混为一谈；本轮未打印或修改 `.env`。

以下是需要真实外部服务/模型的运行示例，不是本轮已执行的验收：

```powershell
# 读取你明确授权的目录；不配 RAG 时使用 --no-knowledge
.venv/Scripts/python.exe -m app.agent 'Read README.md and summarize it' --filesystem-root . --no-knowledge
# 显式选择 Team；默认仍是 single
.venv/Scripts/python.exe -m app.agent 'Review the supplied files' --execution-mode team --filesystem-root . --no-knowledge
```

网页后端（D-046，只在本机、单用户；需先在 `.env` 设 `WEB_ACCESS_TOKEN`，请求带 `Authorization: Bearer <令牌>`）：

```powershell
.venv/Scripts/python.exe -m app.api --filesystem-root . --no-knowledge
```

启动后在浏览器打开 `http://127.0.0.1:8000/`，输入令牌即可使用（D-047）。

`DASHSCOPE_API_KEY`/`RAG_MODEL` 配置主模型；`TAVILY_API_KEY` 启用 WebSearch；Filesystem 写操作还需明确 write root。RAG 需要现有 Milvus/embedding/reranker。Milvus 部署配置在 `deploy/milvus/compose.yaml`。
`CONVERSATION_STORAGE_DIR` 启用会话落盘；`--compress-history` 还需可用的摘要模型。未启用持久化时只是内存模式。

## 3. 核心执行链与阅读入口

```text
CLI / 调用方
  -> RagAgentService：身份、Turn、模型/工具生命周期、暂停恢复
       auto 模式：每个新任务先选模式（宿主规则，再一次模型判断，D-026）
  -> WorkingMemory(TaskState + Resources)
  -> Planner 提议 -> Executor 校验并安装
  -> AgentRuntime 取下一步 / Replan / 完成或阻塞
  -> StepExecutor 开始、校验、提交 Step
  -> RoutingStepRunner -> RunnerRegistry + RoutingPolicy
       -> ToolCallingStepRunner：单 Agent 工具循环
       -> DelegatingTeamRunner（V2）：负责人委派给全新上下文的执行者，宿主判定完成
  -> StepRunOutput -> 公共提交路径 -> Task Completion / Turn outcome
```

| 模块 | 文件 | 职责与不可越界之处 |
| --- | --- | --- |
| 服务装配 | `app/services/rag_agent_service.py` | 创建/恢复会话，按 user/session 隔离，管理请求锁、模型、工具注册、Runtime rebind 与结果记录 |
| 任务状态 | `app/models/taskstate.py`、`working_memory.py`、`resources.py` | 目标/计划/步骤真值，版本与 execution token，证据/上下文资源 |
| 规划 | `app/agent/planner.py`、`planning_model.py`、`executor.py` | LLM 提议结构化计划；可信版本信息由程序绑定；Executor 决定是否安装 |
| 调度 | `app/agent/runtime.py` | 自动推进剩余步骤，处理成功/失败/阻塞、有限执行/Replan；不承担具体工具循环 |
| 单步提交 | `app/agent/step_executor.py`、`step_models.py`、`step_runner.py` | 执行边界、失效结果拒绝、统一输出和异常类别 |
| 路由 | `app/agent/runner_registry.py`、`runner_router.py`、`routing_step_runner.py` | 注册/工厂注入、独立可信能力要求、确定性动态选择；不是 LLM 分类器 |
| 单 Agent | `app/agent/tool_calling_step_runner.py` | 原有有界模型/工具循环和局部 JSON 修复 |
| 适配模型 | `app/agent/openai_compatible_tool_calling_model.py`、`tool_calling_model.py`、`json_output.py` | provider 适配、类型化错误、严格响应解析 |

WorkingMemory 的 goal_revision、plan_revision、state_version 与 Resources.context_version 有不同语义。Planner 旧快照、旧执行 token、旧 rebind 结果不能覆盖新状态。不要绕过 Executor/WorkingMemory 直接安装或提交。

成功输出才完成 Step；失败说明不等于完成。格式修复局限当前尝试且不带业务工具，不重复执行已有工具。工具/业务错误可触发有限 Replan；格式耗尽、provider/配置/路由错误与 Team terminal 错误不应盲重跑。具体分支以当前 runtime/step_runner 源码为准。

## 4. 工具、MCP 与 RAG

统一入口：`app/tools/tool.py`、`tool_registry.py`、`tool_loader.py`。
`open_tool_registry()` 将本地时钟、RAG、外加本地工具与 MCP 发现工具放入同一 Registry；重名报错，不静默覆盖。

MCP 链：`mcp_client.py` → `mcp_tool_loader.py` → `mcp_tool_adapter.py`。`local_mcp_servers()` 用当前 Python 启动 Filesystem/WebSearch stdio 服务。
连接在 `AsyncExitStack` 生命周期内保持有效；创建、执行、关闭同属一个 asyncio task。暂停后重新连接并 rebind，而不是保存活的旧 adapter。

Filesystem：`app/tools/filesystem/file_access.py`、`server.py`；WebSearch：`app/tools/web_search/web_search_service.py`、`models.py`、`server.py`。Tavily 密钥从启动环境传入，不硬编码。

RAG：`knowledge_registry_tool.py` → `rag_search_service.py` → query rewrite、向量/BM25/hybrid、RRF、rerank、context builder；证据进入 Resources。
基础设施在 `vector_embedding_service.py`、`vector_index_service.py`、`vector_store_manager.py`、`app/core/milvus_client.py`。已有 Milvus 全局 alias 兼容逻辑，不能随意断开共享连接。
不要为了长期记忆或 Team 建第二套工具注册/模型框架。`app/agent/aiops`、`mcp_demo` 是旧实现/示例/兼容与实验入口，不是当前主链替代品。

## 5. Memory + Context 的职责划分

| 组件 | 核心代码 | 权威性和内容 |
| --- | --- | --- |
| ConversationTurn/Memory | `app/models/conversation_memory.py` | 一次用户交互及近期完整 Turns、阶段摘要索引；一个 Task 可跨多个 Turn |
| ConversationArchive | `app/services/conversation_archive.py` | 原始用户输入、不可变完成 sidecar、摘要正文、工具 artifact、来源引用；持久化权威 |
| WorkingMemory | `app/models/working_memory.py` | 当前任务的目标、步骤、结果、约束和证据；不是跨会话用户档案 |
| ContextManager | `app/services/context_manager.py` | 从记忆构造预算内模型请求；不修改原始历史和任务真值 |
| LLMCompressor | `app/services/memory_compression/llm_compressor.py` | 由原始 Turns 生成有结构、有来源但有损的摘要 |
| SummaryScheduler | `app/services/summary_scheduler.py` | 有界后台排队、合并通知、重试、超时与关闭 |
| LongTermMemoryService | `app/services/memory_service.py` | 跨会话可复用事实/偏好/项目背景；与会话摘要分开 |

### Turn 生命周期和恢复

先持久化精确用户输入 RECEIVED，再调用模型/工具；最终发布不可变完成 sidecar：COMPLETED/BLOCKED/FAILED/CANCELLED。
request_id 控制同次请求幂等：相同身份/原文/映射可回放结果，冲突拒绝；未完成原文不能自动重放不确定的外部操作。
BLOCKED 只结束这次交互。保持同一个服务实例，使用 `resume_task_id`（必要时 `question_id`）回答，形成关联同一 Task 的新 Turn。
重启可恢复历史和已完成结果，不能重建运行中 WorkingMemory/Team 执行检查点。`clear()` 清视图，不删磁盘。
原文 v1 与新 v2/sidecar 保持兼容；并发保障是单进程多实例锁，不是跨进程写者协调或抵抗恶意文件改写。

### 已落地的摘要和上下文

- add/save 的 I/O 优化复用元数据，近期/待摘要正文读取有界；文件枚举、stat/排序仍随历史增长，不是 O(1)。
- 近期默认保留 20 Turns；较老终结交互按 Task 分批，从原文生成摘要，不递归摘要旧摘要；RECEIVED 不覆盖为已摘要。
- 后台默认并行 2、队列 64、批次最多 8 Turns/估算 6000 tokens；最多 2 次尝试、有界关闭。生成不占会话锁，提交前校验来源/版本。
- 摘要保留 memory_id/title/description/storage_ref/source_turn_ids/session_id 和可选 task_id；失真可回查原文。
- ContextManager 自动加载相关摘要正文：默认至多 3 候选、2048 估算 tokens，并受总窗口约束；词法/CJK bigram 相关性不是语义检索。
- 请求预算包含 system、工具 schema、历史、结果和输出/安全预留；大型工具结果可外置并留预览/引用，按权限读取。
- 上下文超限按规则减小可选投影，再有限重试模型，不重放工具；token 数是估计，不是供应商精确 tokenizer。

已知限制：超大单 Turn 不自动切分，最老失败批次可阻塞后续摘要；只恢复已加载会话的后台工作。摘要语义和语言保持依赖模型，结构校验不证明事实完整。
本地摘要工厂在 `app/core/dependencies.py`，默认 `qwen3:4b-instruct`/Ollama。历史真实验证遇到缺失 GGUF 权重，显式注入远程模型完成实验；当前权重健康本轮未重测，不自动切换默认值。
三级 warm body cache 仍是设计，不要直接按设计文档实现。

## 6. 长期记忆：已有能力与未完成接入

阅读顺序：`app/models/longterm_memory.py` → `app/services/extrator.py`（保留现有拼写）→ `memory_store.py`/`postgres_memory_store.py` → `memory_vector_index.py` → `memory_retriever.py` → `memory_resolver.py` → `memory_service.py`。

- Extractor 一段 text 可产出 `list[MemoryCandidate]`；来源字段由调用方绑定。Prompt 已要求重要、明确、可长期保存，可返回空列表，但不是独立校准过的价值/质量门禁。
- Resolver 对候选与已检索记忆作 ADD/UPDATE/SKIP/CONFLICT；目标 ID 必须属于检索历史，冲突不静默覆盖。
- PostgreSQL 保存 MemoryRecord，包含 user_id、版本、来源和时间；更新使用乐观版本校验。
- Milvus 独立 `agent_memories` Collection，memory_id 主键、用户过滤、L2 距离、初始化兼容校验；连接/embeddings 由外层拥有。
- Retriever 并行 keyword/vector 后 RRF，回查 PostgreSQL，过滤过期/缺失/跨用户记录；score 是排名权重，不是可信度。
- Service 顺序处理候选，数据库先写再更新索引；批次不原子。MemoryIndexSyncError 标明 operation/database_changed，取消继续传播；不能自动回放 remember。

当前脏源码已含 `RagAgentService(longterm_memory=..., longterm_recall_limit=5, longterm_timeout_seconds=3.0)`、显式 user_id、身份隔离、recall 超时降级和 ContextManager.longterm_context 投影；冻结 HEAD 不含这些改动。
**不能因此说 Agent 长期记忆接入已完成验收**：入口尚需外层装配已初始化依赖；CLI 没有默认构造长期记忆；当前未见 Agent 服务 `remember_explicit` 接口。此前计划的 Tasks 1–4 不能统称已交付。
当前 longterm_context 默认预算 100 估算 tokens，可能使很多完整条目无法进入上下文；这是需用测试验证的风险，不是本轮已复现故障。

已通过受控 stand-in 复现的跨库边界：数据库较新更新之后，较旧索引写可能晚到；forget 后也可能出现迟到旧向量。CAS 不排序后续 Milvus 写。Retriever 回查权威行可挡住孤儿内容返回，但不能保证召回正确或索引同步。无 outbox/跨库事务或通用补偿；本轮不修业务代码。

## 7. Agent Team（V2；V1 已于 2026-11-05 删除）

`app/agent/team_v2.py` 注册 runner `agent_team`。团队模式下服务用 `SingleStepPlanner` 只产生一步计划，整件任务交给团队。负责人只能调用 `delegate`（可选 `review`；开启独立验收时还有 `verify`），每次委派都新建一个普通模式执行器（`ToolCallingStepRunner`），只拿到授予的工具，上下文是全新的。

完成由宿主判定：开启沙盒时要求最后一次写入之后有一次通过的 `run_tests`/`run_python`；工具结果由宿主自己记录，执行者最终报告出错也不丢证据（D-023）。独立验收默认关闭，`--team-acceptance` 开启（D-022）。执行者上下文按两档压缩，防止超窗（`model_view.py`）。

默认上限（`DelegationOptions`）：6 次委派、2 次验收、负责人 16 轮、执行者 12 轮/24 次工具调用、180 秒。`app/agent/team/` 只剩 `models.py`（`TeamError`、`WORKER_ONLY_TOOLS`、通过门槛函数）和 `diagnostics.py`（安全异常类型链）。

真实评测和已知短板见 [V2 实现记录](harness/team-v2-2026-11-04.md)；V1 的设计与证据见历史报告和提交 `e20a6e1` 及更早版本。

## 8. 证据矩阵：不要把机制成功当成质量优势

以下是原始文档中的历史结果。本轮未重新调用模型或运行数据库；详细配置与失败样本以原报告为准。

| 对象 | 已记录证据 | 解释边界 |
| --- | --- | --- |
| 冻结候选/工作区 | 1093 passed/2 skipped，独立候选也通过；清理前再跑 1093/2 | 原 tests 已删除（现仅最小 harness 测试）；不是本轮 fresh regression |
| Multi-Runner 真连接 | 多工具、暂停/重连、Replan 三场景通过 | Replan 有验证专用 fail-fast 钩子；不是一般成功率 |
| 摘要正文真实使用 | 合成任务末步骤 6/6；严格最终 JSON 5/6；禁正文对照 0/6 完全正确 | 一条语言不符，局部末步骤正确不等于最终交付正确 |
| Team 合成完整链 | 完成、暂停恢复、显式种错后的审查返工通过 | seeded negative control 不是自然发现错误能力 |
| Reviewer 可靠性单例 | 一次真实审查完成，过长反馈被拒并修复，Worker v2 审批 | 未出现 provider length；截断修复只确定性验证，结论仍有误报 |
| 最近 3 组真实只读维护任务 | 普通 2/3，Team 0/3；42 请求；11 MCP scope 关闭；无同成员超额重读 | Reviewer 超长/修复耗尽和 JSON 是已观察瓶颈，无 Team 准确率优势 |
| 摘要性能 | 受控前台额外约 31ms 中位/35ms P95；远程小样本正文选择额外约 5ms 中位 | 不是本机 Ollama 并发推理或生产 SLA |
| 本轮 Harness | 只读入口、文档/状态/保护校验 | 只证明交接组织与当前静态入口，不证明 Agent 运行质量提升 |

较早 I/O 报告的 7 个 math 失败属于当时基线，后续任务已修；不能据旧文档称当前仍有这 7 个失败。较早“摘要正文未接入”“只有一个 Runner”“PoC 不接生产”均是阶段事实，被后续阶段接续。

## 9. 当前风险和建议下一步（不是自动授权）

1. **先保护可验证性**：TEMP 外部备份不是长期存储；在后续业务改动前确认可恢复必要测试和实验协议，勿仅靠语法检查。
2. **优先 Team 交付可靠性**：以冻结的真实任务为基线，定位 Reviewer 反馈修复/内层 JSON，保留严格宿主契约、预算和不重放约束；修复另成任务，不扩团队组件。
3. **长期记忆接入收尾**：先测试身份隔离、recall 降级、预算和外层装配，再考虑显式记忆写入；不先堆质量评分或换检索算法。
4. **暂缓架构扩张**：不实施 warm body cache、复杂并行、跨进程恢复、新 Agent 框架或复杂记忆系统，除非新任务给出具体证据和验收。

## 10. 文件保留、恢复和接手提示

全部原 docs 保留；[文档索引](DOCUMENT_INDEX.md) 指明当前/历史/设计状态。
tests/scripts/tmp 备份：`C:\Users\48174\AppData\Local\Temp\Aletheia-cleanup-jjfezytd\removed.zip`，相邻 manifest 有哈希。本轮确认备份可读、目录列出 68,384 条且包含 Team 测试；本轮未重新逐文件校验 zip 内容，逐文件校验是上次清理时的证据。未全量恢复，敏感实验日志不能随交接包公开上传。已跟踪测试/脚本另可从本地冻结提交查阅历史版本；未跟踪实验产物不能只靠 Git 恢复。
10 个权限阻塞的 tmp 目录残留曾在清理中记录，本轮不改 ACL。详见 [清理记录](project_cleanup.md)。
旧 AGENTS/功能表/进度/交接已存入 [管理状态归档](harness/history/2026-11-04-state-before.json)。当前入口是 [AGENTS.md](../AGENTS.md)，操作规范见 [Harness 手册](harness/README.md)。

可以给 Claude 的开场指令：

> 先读 CLAUDE.md、AGENTS.md、session-handoff.md 和 docs/PROJECT_HANDOVER.md，查看 Git 状态并运行 init.ps1。保留所有用户源码差异和文档。报告当前状态及验证边界，然后只执行我下一条明确指定的任务；不要把历史测试结果当作当前通过，不自动调用付费 API 或提交代码。
