# Aletheia — Agent Engineering Harness

本文件是 Codex/Claude 的短入口。项目事实见交接文档，当前工作见状态文件，历史报告不自动成为本轮指令。用户最新要求优先。

## Startup Workflow

1. 查看 `git status --short` 和 `git log -1 --oneline`；保留已有修改、删除和未跟踪文件。
2. 阅读交接文件：`.handoff/session-handoff.md`、`.handoff/feature_list.json`、`.handoff/progress.md`。它们不在 `main` 上，只在独立的 `handoff` 分支里（用户 2026-11-12 决定，D-077），检出在仓库内的 `.handoff/` 工作树（已被 `.gitignore` 忽略）。没有这个目录时先运行 `git fetch origin handoff` 和 `git worktree add .handoff handoff`；已有时在 `.handoff/` 里 `git pull`。
3. 首次接手读 [项目总交接](docs/PROJECT_HANDOVER.md) 和 [决策记录](docs/DECISIONS.md)，以后按 [文档索引](docs/DOCUMENT_INDEX.md) 按需读取。
4. 在根目录运行只读健康检查。失败先记录，不自动安装依赖或改配置来制造通过。
5. 明确目标、涉及文件和验收。One feature at a time；待办建议不代表授权。

## Verification Commands

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
```

做管理文件、JSON、Python compile、CLI 加载检查，并运行确定性 pytest 套件（basetemp 放在 TEMP）；不执行模型、MCP、数据库或完整任务。启动时再运行基线核对（`.handoff/docs/harness/baseline.md`）。
原 tests/ 和验证脚本曾按用户要求删除；当前 `tests/` 已恢复历史测试（D-030），init.ps1 运行它们（2026-11-11 Windows 为 1731 项通过、16 项跳过；2026-11-12 云端 Linux 为 1785 项通过、22 项跳过、4 项只在 Windows 上成立的路径断言失败，Windows 上待重跑；真实容器测试需设 `ALETHEIA_DOCKER_TESTS=1`，历史开启记录为 1231 项通过，本轮未运行）。历史 1093/2 不是当前回归。后续行为修改须恢复对应测试或在 tests/ 中补充测试；参见 [清理记录](docs/project_cleanup.md)。

## Global Invariants

- 一个任务一个循环：`LoopRun` 负责取消、超时、重新绑定、版本校验、预算和结果提交；执行器（`AgentLoopRunner`）只返回输出，不直接改权威状态（D-076，取代原“保留 Plan–Execute–Replan”）。
- 团队是单一循环里的 `delegate` 工具；成员是独立上下文的子循环，不嵌套 Runtime；完成由宿主判定（D-075）。
- 执行方式由宿主选择（`route_selection` 的宿主规则在前，模型只能在规则允许的范围内选），模型不能放宽；修改状态语义或执行器接口（`runner_factory`）前先说明兼容影响。
- 宿主拥有最终输出契约和验收门；Lead 内部要求、Reviewer 意见及 Member 协议不能覆盖它。
- 保留取消、超时、rebind、版本校验与预算上限；格式修复不得重放完成的工具；外部副作用不确定时不得自动重试。
- MCP 连接覆盖执行期，进入/退出须在拥有它的同一 asyncio task；暂停后不能复用已关闭适配器。
- Archive 保留原文、sidecar、顺序、幂等、原子发布、旧格式读取及 session/path 隔离；clear 不是磁盘删除。
- ConversationMemory 管会话视图，`RunState` 管任务真值，ContextManager 只投影；摘要不能覆盖当前指令或原文。
- 长期记忆 PostgreSQL 权威、Milvus 为独立可重建索引；保留用户隔离与版本校验；索引失败不得盲重跑 remember。
- 不自动改 .env、模型默认值、依赖或业务数据，不安装/下载，不自动提交、amend、推送或发布；按本轮实际授权执行。
- 不记录密钥、完整私有对话、provider 原文或不受控异常；历史 API 授权不扩大到新材料或无限调用。
- 不宣称跨进程执行恢复、通用工具 exactly-once、可靠事实验证或 Team 质量优势；证据见总交接。

## Scope and Evidence

- `.handoff/` 里的 feature_list.json 记录功能 status、依赖与证据；session-handoff.md 记录当前任务和下一步；progress.md 记录执行结果；基线清单也在那里。这些文件只用于 Agent 之间交接，绝不提交到 `main`。
- 当前代码/命令优先于旧计划；历史报告解释决策。冲突须查证，不能沿用旧“运行中/未实现”。
- 按主题读取，不默认加载全部 docs 或历史归档；区分冻结 HEAD 与当前脏工作区。
- 多 Agent 工作先划清所有权与合并方式，不默认并发写同一文件。

## Definition of Done

- 用户要求逐项有对应文件或执行证据；范围受控，已有未提交内容保留。
- 运行适用检查，区分 static/import、确定性测试、真实服务、真实模型 E2E；缺失项标为未验证。
- 零测试、跳过检查、结构评分或正确的中间草稿均不能证明任务完成；失败同样记录。
- 同步功能状态、交接与进度，记录风险和下一步；没有本轮 Git 授权不提交。

## End of Session / Next steps

更新 `.handoff/` 里的 feature_list.json、progress.md、session-handoff.md，附命令和证据位置，在 `.handoff/` 里提交并推送 `handoff` 分支（只放交接文件；推送仍按本轮授权）。新决策写入 `main` 的 docs/DECISIONS.md。再检查两边的 Git 状态。
逐项完成 [clean-state 清单](docs/harness/clean-state-checklist.md)；需要评审时用 [evaluator-rubric](docs/harness/evaluator-rubric.md)。
Clean handoff 指状态清楚、可恢复，不要求清空用户脏工作区；历史只归档，不覆盖当前交接。
维护规则见 [Harness 手册](docs/harness/README.md)。
