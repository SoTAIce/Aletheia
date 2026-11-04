# 质量文档（Quality Document）

更新：2026-11-04。课程模板按领域和架构层评 A–D。评级依据是仓库内已有证据，不代表新测量；每次周期性清理时复核。

评级含义：**A** 当前有 fresh 验证且边界清晰；**B** 有历史验证、文档清楚；**C** 部分实现或只有受控验证；**D** 缺验证或缺接入。
全局事实：原 tests/ 已删除，当前仓库内只有 WorkingMemory 2 个和 Team 契约 8 个测试，所以“当前测试稳定性”除主链和 Team（C）外都是 **D**。Team 另有 68 个历史测试可从 git 取出、在 TEMP 中运行（[审查](team-audit-2026-11-04.md)）。下表“验证状态”反映的是历史证据。

## 产品领域

| 领域 | 验证状态 | Agent 可读性 | 当前测试稳定性 | 关键缺口 | 主要证据 |
| --- | --- | --- | --- | --- | --- |
| Plan–Execute–Replan 主链 | B | A | C | 只有 WorkingMemory 最小测试 | [agent_runtime.md](../agent_runtime.md), [working_memory_v1.md](../working_memory_v1.md) |
| Multi-Runner 路由 | B | A | D | Replan 真实验收用了 fail-fast 钩子 | [multi_runner_live_validation.md](../multi_runner_live_validation.md) |
| Agent Team V1 | C | A | C | 真实任务 0/3 严格交付；Reviewer 长反馈和 JSON 是瓶颈 | [agent_team_task_comparison_2026-11-03.md](../agent_team_task_comparison_2026-11-03.md) |
| 会话归档 / Turn 生命周期 | B | A | D | 不能跨进程恢复执行 | [conversation_turn_lifecycle.md](../conversation_turn_lifecycle.md) |
| 摘要与 Context 投影 | B | B | D | 摘要语义依赖模型；本地 Ollama 权重健康未重测 | [conversation_summary_context.md](../conversation_summary_context.md) |
| 长期记忆 | C | B | D | 未提交的接入代码未验收；跨库迟到写；CLI 未装配 | [PROJECT_HANDOVER §6](../PROJECT_HANDOVER.md) |
| 工具 / MCP / RAG | B | B | D | 依赖真实 Milvus/embedding，本轮未运行 | [PROJECT_HANDOVER §4](../PROJECT_HANDOVER.md) |

## 架构层

| 层 | 边界执行 | Agent 可读性 | 说明 |
| --- | --- | --- | --- |
| CLI（`app/agent/__main__`） | B | A | init.ps1 验证 `--help`；默认不装配长期记忆 |
| 服务装配（`rag_agent_service.py`） | B | B | 工作区有未提交改动，与冻结 HEAD 不同 |
| 状态（`app/models/`） | A | A | 版本/token 语义有文档；具体规则在 WorkingMemory/Executor 中执行 |
| 执行/路由（`app/agent/`） | A | A | 提交门在 StepExecutor；能力来自可信来源（DECISIONS D-003） |
| 工具/MCP（`app/tools/`, `mcp_*`） | B | B | 连接生命周期绑定同一 asyncio task |
| 存储（Archive, PostgreSQL, Milvus） | B | B | 跨库不原子（DECISIONS D-006） |

## 可观测性（课程 L11）

| 课程项 | 现状 |
| --- | --- |
| 生命周期 / 错误上下文 | 部分：`logging`（coordinator、context_manager、rag_agent_service、summary_scheduler）与 `loguru`（MCP、Milvus、向量服务）并存；Team 有 `team/diagnostics.py` 安全诊断 |
| 每个任务可重放的 trace | 部分：Turn sidecar 和 WorkingMemory 记录结果，但没有统一 trace |
| 标准 schema（OpenTelemetry：每会话一个 trace，每任务一个 span） | 没有 |
| 过程层：任务契约、rubric、版本化进度 | 有：手册中的任务契约模板、[evaluator-rubric](evaluator-rubric.md)、progress/feature_list |

建议（未授权，不自动执行）：统一日志库，以 Turn/Task/Step ID 作为关联键，再考虑 OTel。按 DECISIONS 的惯例，要先给出具体证据和验收标准。
