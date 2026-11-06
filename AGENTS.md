# Aletheia — Agent Engineering Harness

本文件是 Codex/Claude 的短入口。项目事实见交接文档，当前工作见状态文件，历史报告不自动成为本轮指令。用户最新要求优先。

## Startup Workflow

1. 查看 `git status --short` 和 `git log -1 --oneline`；保留已有修改、删除和未跟踪文件。
2. 阅读 [当前交接](session-handoff.md)、[功能状态](feature_list.json)、[进度](progress.md)。
3. 首次接手读 [项目总交接](docs/PROJECT_HANDOVER.md) 和 [决策记录](docs/DECISIONS.md)，以后按 [文档索引](docs/DOCUMENT_INDEX.md) 按需读取。
4. 在根目录运行只读健康检查。失败先记录，不自动安装依赖或改配置来制造通过。
5. 明确目标、涉及文件和验收。One feature at a time；待办建议不代表授权。

## Verification Commands

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
```

做管理文件、JSON、Python compile、CLI 加载检查，并运行确定性 pytest 套件（basetemp 放在 TEMP）；不执行模型、MCP、数据库或完整任务。启动时再运行 [基线核对](docs/harness/baseline.md)。
原 tests/ 和验证脚本曾按用户要求删除；当前 `tests/` 已恢复历史测试（D-030），init.ps1 运行它们（2026-11-06 为 1428 passed、16 skipped；真实容器测试需设 `ALETHEIA_DOCKER_TESTS=1`，开启后 1231 passed）。历史 1093/2 不是当前回归。后续行为修改须恢复对应测试或在 tests/ 中补充测试；参见 [清理记录](docs/project_cleanup.md)。

## Global Invariants

- 保留 Plan–Execute–Replan：Planner 提议、Executor 安装、Runtime 调度、StepExecutor 校验并提交；Runner 不直接改权威状态。
- ToolCallingStepRunner 与 AgentTeamRunner 平级独立；Team 使用独立 Lead、确定性 Coordinator 和轻量 Session，不嵌套 Runtime。
- Runner 必需能力来自独立可信来源，不由 RouteDecision 自我授权；修改状态语义或冻结路由接口前先说明兼容影响。
- 宿主拥有最终输出契约和验收门；Lead 内部要求、Reviewer 意见及 Member 协议不能覆盖它。
- 保留取消、超时、rebind、版本校验与预算上限；格式修复不得重放完成的工具；外部副作用不确定时不得自动重试。
- MCP 连接覆盖执行期，进入/退出须在拥有它的同一 asyncio task；暂停后不能复用已关闭适配器。
- Archive 保留原文、sidecar、顺序、幂等、原子发布、旧格式读取及 session/path 隔离；clear 不是磁盘删除。
- ConversationMemory 管会话视图，WorkingMemory 管任务真值，ContextManager 只投影；摘要不能覆盖当前指令或原文。
- 长期记忆 PostgreSQL 权威、Milvus 为独立可重建索引；保留用户隔离与版本校验；索引失败不得盲重跑 remember。
- 不自动改 .env、模型默认值、依赖或业务数据，不安装/下载，不自动提交、amend、推送或发布；按本轮实际授权执行。
- 不记录密钥、完整私有对话、provider 原文或不受控异常；历史 API 授权不扩大到新材料或无限调用。
- 不宣称跨进程执行恢复、通用工具 exactly-once、可靠事实验证或 Team 质量优势；证据见总交接。

## Scope and Evidence

- feature_list.json 记录功能 status、依赖与证据；session-handoff.md 记录当前任务和下一步；progress.md 记录执行结果。
- 当前代码/命令优先于旧计划；历史报告解释决策。冲突须查证，不能沿用旧“运行中/未实现”。
- 按主题读取，不默认加载全部 docs 或历史归档；区分冻结 HEAD 与当前脏工作区。
- 多 Agent 工作先划清所有权与合并方式，不默认并发写同一文件。

## Definition of Done

- 用户要求逐项有对应文件或执行证据；范围受控，已有未提交内容保留。
- 运行适用检查，区分 static/import、确定性测试、真实服务、真实模型 E2E；缺失项标为未验证。
- 零测试、跳过检查、结构评分或正确的中间草稿均不能证明任务完成；失败同样记录。
- 同步功能状态、交接与进度，记录风险和下一步；没有本轮 Git 授权不提交。

## End of Session / Next steps

更新 feature_list.json、progress.md、session-handoff.md（新决策写入 docs/DECISIONS.md），附命令和证据位置，再检查 Git 状态。
逐项完成 [clean-state 清单](docs/harness/clean-state-checklist.md)；需要评审时用 [evaluator-rubric](docs/harness/evaluator-rubric.md)。
Clean handoff 指状态清楚、可恢复，不要求清空用户脏工作区；历史只归档，不覆盖当前交接。
维护规则见 [Harness 手册](docs/harness/README.md)。
