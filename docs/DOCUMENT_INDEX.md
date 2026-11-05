# Aletheia 文档索引

更新：2026-11-04。先读 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)，此处用于按需追溯原文。
**历史**代表当时范围/结果，不自动成为现状；**设计**不表示已实现。文中的 tests/scripts/tmp 路径很多已在清理中外部归档，执行前先看 [清理记录](project_cleanup.md)。原始文件不覆盖。

## 当前入口

| 文件 | 用途 |
| --- | --- |
| [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) | 给 Claude 的系统总交接：核心模块、现状、验收、风险、下一步 |
| [Harness 手册](harness/README.md) | 管理闭环、状态字段与课程映射、任务契约、验证分级及方法来源 |
| [DECISIONS.md](DECISIONS.md) | 设计决策、理由、被否方案（来源指向原报告） |
| [clean-state 清单](harness/clean-state-checklist.md)、[evaluator-rubric](harness/evaluator-rubric.md)、[质量文档](harness/quality-document.md) | 会话退出检查、评审打分、领域/层质量与可观测性 |
| [合规审查 2026-11-04](harness/conformance-audit-2026-11-04.md) | 对照 Learn Harness Engineering 的逐项结论与剩余缺口 |
| [能力增强阶段 1 2026-11-04](harness/capability-phase1-2026-11-04.md) | 工具参数错误可有界修复、失败带类型链；评测集 bench/（3 任务 + 自动评分）；确定性测试 20 个，历史 450 passed |
| [评测集说明](../bench/README.md) | 任务、运行命令、授权与上限、判定口径 |
| [single/team 前端任务对照 2026-11-04](harness/frontend-compare-2026-11-04.md) | 真实模型各跑 1 次单文件 HTML 任务：single、team 都失败（0/1），未产出 HTML |
| [Team 一致性审查 2026-11-04](harness/team-audit-2026-11-04.md) | Team V1 文档声明逐条对照代码；历史 Team 测试 68 passed；修订上限表述更正 |
| [已记录基线](harness/baseline.md) | HEAD + 工作区差异哈希清单（[JSON](harness/history/baseline-2026-11-04.json)）及核对命令 |
| [AGENTS.md](../AGENTS.md)、[CLAUDE.md](../CLAUDE.md) | 公共工程约束与 Claude 入口 |
| [session-handoff.md](../session-handoff.md)、[progress.md](../progress.md)、[feature_list.json](../feature_list.json) | 当前状态、执行记录、功能证据 |

## Agent 执行与路由

| 原始文档 | 内容与当前解释 |
| --- | --- |
| [agent_runtime.md](agent_runtime.md) | 主执行链、结果/异常/格式修复；较早示意图只画单 Runner，Team 见后续文档 |
| [working_memory_v1.md](working_memory_v1.md) | 状态、版本、资源和提交边界；了解任务真值优先读它 |
| [executor_v1.md](executor_v1.md) | Planner 提案/Executor 安装的历史适配说明；部分文本已有乱码，精确接口核对源码 |
| [multi_runner_infrastructure.md](multi_runner_infrastructure.md) | 注册/能力/路由/DI/取消与 rebind，V1 冻结；随后 Team 通过既有扩展点接入 |
| [multi_runner_live_validation.md](multi_runner_live_validation.md) | 三个真实模型+MCP 场景、独立提交与失败 pilot；Replan 使用验证专用 fail-fast 钩子 |

## Conversation / Context / 压缩

| 原始文档 | 内容与当前解释 |
| --- | --- |
| [conversation_memory_io_results.md](conversation_memory_io_results.md) | 元数据复用、有界正文读取、性能对比；当时 7 个基线失败后已修，不是当前故障 |
| [conversation_memory_tiers.md](conversation_memory_tiers.md) | hot/warm/cold 设计和停止条件；warm body cache 未实现，结论是暂缓 |
| [conversation_turn_lifecycle.md](conversation_turn_lifecycle.md) | 原始输入、Task/Turn、状态 sidecar、请求幂等、同服务暂停恢复 |
| [conversation_summary_background.md](conversation_summary_background.md) | Task 分组摘要、后台队列、重试/关闭；开头“正文接入 deferred”已由下一阶段接续 |
| [conversation_summary_agent_validation.md](conversation_summary_agent_validation.md) | 10 个受控 Agent 生命周期场景；不是当时的真实模型验证 |
| [conversation_summary_latency.md](conversation_summary_latency.md) | 离线受控前台延迟；非真实推理并发或生产 SLA |
| [conversation_summary_context.md](conversation_summary_context.md) | 自动摘要正文、预算和真实合成召回对照；5/6 严格 JSON、缺失本地权重与语言偏差均保留 |

## Team：机制、集成、真实任务

| 原始文档 | 内容与当前解释 |
| --- | --- |
| [team_session_poc.md](team_session_poc.md) | 历史单轮 Session/中途协调实验；其严格 JSON 失败没有被后续成功抹去 |
| [team_contract_review_poc.md](team_contract_review_poc.md) | 宿主契约、多轮协调与种错返工实验；后被提升为生产 Team 机制 |
| [agent_team_v1.md](agent_team_v1.md) | 当前 Team 的独立 Runner、Lead/Coordinator/Session、权限预算与暂停边界 |
| [agent_team_real_review_2026-11-03.md](agent_team_real_review_2026-11-03.md) | 真实审查失败、命令修正、Reviewer 长输出/异常修复和单例完成；不是质量优势证明 |
| [agent_team_v1_freeze.md](agent_team_v1_freeze.md) | 本地 9138ac1 的依赖闭包、候选测试和明确排除的长期记忆工作区改动 |
| [agent_team_task_comparison_2026-11-03.md](agent_team_task_comparison_2026-11-03.md) | 后续三组真实对照最终结论：普通 2/3、Team 0/3；42 请求与资源关闭，不可换成中间稿得分 |

## 实施计划（按时间，不是自动待办队列）

| 原始计划 | 状态解释 |
| --- | --- |
| [2026-10-17 memory-context](superpowers/plans/2026-10-17-memory-context.md) | P0/P1 记忆与上下文历史计划；落地状态看后续专题报告和源码 |
| [2026-10-21 longterm-agent-integration](superpowers/plans/2026-10-21-longterm-agent-integration.md) | 部分源码接入，未整体验收；依赖装配、显式写入不能标为已交付 |
| [2026-10-25 conversation-io](superpowers/plans/2026-10-25-conversation-io.md) | 已完成 I/O 优化与三级缓存设计；不等于三级缓存实施 |
| [2026-10-27 turn-lifecycle](superpowers/plans/2026-10-27-turn-lifecycle.md) | 已完成持久交互生命周期与同进程恢复 |
| [2026-10-30 summary-background](superpowers/plans/2026-10-30-summary-background.md) | 已完成阶段摘要/后台调度 |
| [2026-10-31 summary-context](superpowers/plans/2026-10-31-summary-context.md) | 已完成摘要正文消费和受控真实验证，保留失败 |
| [2026-10-31 multi-runner](superpowers/plans/2026-10-31-multi-runner.md) | 第一阶段注册/路由基础设施完成 |
| [2026-10-31 runner-live-commit](superpowers/plans/2026-10-31-runner-live-commit.md) | 三场景真实验收与 adea10e 本地提交完成 |
| [2026-11-02 team-session-poc](superpowers/plans/2026-11-02-team-session-poc.md) | 历史实验完成，输出失败由下一 PoC 处理 |
| [2026-11-02 team-contract-review](superpowers/plans/2026-11-02-team-contract-review.md) | 契约与有限多轮协作 PoC 已完成 |
| [2026-11-02 agent-team-v1](superpowers/plans/2026-11-02-agent-team-v1.md) | 生产 Runner 集成完成；不表示实际任务稳定 |
| [2026-11-03 team-v1-freeze-evaluation](superpowers/plans/2026-11-03-team-v1-freeze-evaluation.md) | 冻结及三组对照已完成，最终结果见对照报告 |
| [2026-11-04 project-harness](superpowers/plans/2026-11-04-project-harness.md) | 本轮管理与交接整理计划；结果见当前状态/验证记录 |

## 清理、归档与本次验证

| 文件 | 注意事项 |
| --- | --- |
| [project_cleanup.md](project_cleanup.md) | 测试/脚本/临时产物清理与外部 TEMP 备份；10 个权限残留；原报告不改写 |
| [旧管理状态](harness/history/2026-11-04-state-before.json) | 原 AGENTS/feature/progress/handoff 和保护哈希；历史参考，勿当活动指令 |
| [本轮验收](harness/validation-2026-11-04.md) | 只读检查、链接覆盖、源码与原文保护及证据限制 |

维护：新增 docs 应增加索引，重大事实变化先更新总交接；计划保留当时上下文，新阶段明确其接续关系，避免删除负面实验或把旧授权当新授权。
