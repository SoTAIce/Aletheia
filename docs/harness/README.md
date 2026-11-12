# Aletheia 工程 Harness

更新：2026-11-04。目标是让 Codex/Claude 从仓库恢复真实状态、限制改动范围并提供可核查交付；不改变应用 Agent 架构。

## 文件职责与读取次序

| 文件 | 唯一职责 | 何时读取/更新 |
| --- | --- | --- |
| [AGENTS.md](../../AGENTS.md) | 短入口、全局不变量、验收纪律 | 每个会话启动；约束变化时更新 |
| [CLAUDE.md](../../CLAUDE.md) | 引导 Claude 到同一入口 | Claude 启动；不复制另一套规则 |
| `session-handoff.md`（`handoff` 分支） | 当前任务、阻塞、下一步 | 每轮启动/结束；当前段覆盖旧状态 |
| `feature_list.json`（`handoff` 分支） | 功能 ID、依赖、status、证据与待办 | 选择任务和完成验收时 |
| `progress.md`（`handoff` 分支） | 精简执行日志、命令结果 | 每阶段结束追加 |
| [PROJECT_HANDOVER.md](../PROJECT_HANDOVER.md) | 系统架构、模块入口、已知边界和结论 | 首次接手/跨模块工作；系统事实变化时 |
| [DOCUMENT_INDEX.md](../DOCUMENT_INDEX.md) | 原始报告/计划及版本关系 | 深入具体模块时 |
| [init.ps1](../../init.ps1) | Windows 只读健康检查，打印标准启动/验证路径 | 启动与本轮交付前 |
| [DECISIONS.md](../DECISIONS.md) | 设计决策：理由、被否方案、约束 | 首次接手；做出或推翻决策时追加 |
| [clean-state-checklist.md](clean-state-checklist.md) | 会话退出清单 | 每轮结束 |
| [evaluator-rubric.md](evaluator-rubric.md) | 6 维 0–2 分评审，Accept/Revise/Block | 任务验收、周期复核 |
| [quality-document.md](quality-document.md) | 领域/层 A–D 质量评级与可观测性现状 | 周期性清理时复核 |
| [合规审查](conformance-audit-2026-11-04.md) | 与课程的逐项对照和剩余缺口 | 调整 harness 时 |
| `docs/harness/baseline.md`（`handoff` 分支） | 已记录基线：HEAD + 工作区差异哈希清单 + 核对命令 | 启动核对；未提交时每轮结束刷新 |

2026-11-12 起（D-077），交接文件（session-handoff.md、feature_list.json、progress.md）、基线说明和 `history/` 下的清单与状态存档都移到独立的 `handoff` 分支，不再放在 `main` 上；检出到仓库内的 `.handoff/`（已被 `.gitignore` 忽略），方法见 AGENTS.md。`main` 上旧文档里指向这些文件的链接是历史记录，去 `handoff` 分支的同名路径查看。历史管理文件见该分支的 `docs/harness/history/2026-11-04-state-before.json`，用于追溯，不是活动指令。旧 docs 原文不覆盖；其中过时表述通过索引和总交接纠正，不静默改写实验历史。

## 工作闭环

1. 初始化：读入口/交接、查看 Git、运行健康检查，区分当前工作区与冻结提交。
2. 收敛任务：从本轮要求选一个功能，记录范围、依赖和验收；最多一个 active_feature。
3. 执行：先读实现，最小修改；工具权限、外部调用依据本轮授权。
4. 验证：按影响选择证据级别；保留失败，不放宽断言或隐藏重跑。
5. 交接：更新状态、结果、阻塞和下一步；不自动提交或清空工作区。

## 功能状态约定

保留原 features 和历史 evidence。`status=done` 只表示该条交付已完成，不代表语义质量或当前全回归通过。
新增任务至少有 `id/name/description/status/dependencies/acceptance/evidence`；未完成的 evidence 可记录阶段事实，不伪造成功。
推荐状态：planned、in_progress、blocked、partial、done、deferred。
顶层 active_feature 最多一个，null 表示等待下一项授权；backlog 建议中 authorized=false 不自动启动。
verification_context 标注测试是否可用及当前验证范围。删除测试不等于撤销实现；历史通过不等于当前通过。

与课程状态词汇（L08 / Templates）的映射：

| 课程 | 本项目 | 说明 |
| --- | --- | --- |
| not_started | planned / backlog | backlog 中 authorized=false 的条目不会自动启动 |
| in_progress / active | in_progress（同时最多一个，等于 active_feature） | init.ps1 会校验 |
| blocked | blocked | 写明阻塞原因 |
| passing | done，**且**本轮 verification 命令实际运行通过 | 历史条目的 `evidence_is_historical=true` 表示“曾通过”，不等于当前 passing |
| — | partial / deferred | 课程没有对应状态；partial 必须写明缺什么 |

粒度：每条功能要能在一个会话内完成。backlog 条目开工前先拆分。状态只能凭证据改，不能凭感觉改。

## 任务契约模板（开工前写进 session-handoff 的 Scope）

```text
Feature: <id>
Scope: <涉及文件/模块>
Exclusions: <明确不做/不碰的内容>
Verification: <命令 + 证据级别（static / 确定性 / 真实服务 / E2E）>
Authorization: <本轮允许的 API/DB/提交范围>
Done when: <可观察的验收条件>
```

## 验证分级与命令

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
```

入口校验管理文件、功能 ID/依赖、完成证据字段、所有 app Python 语法和 CLI 导入/参数解析，再运行 pytest。不写 pyc，不调用模型/数据库/MCP，不加载模型权重。
它不会自动证明文档内容准确、链接有效或业务行为正确；本次整理另做这些审查。

| 级别 | 能证明什么 | 当前使用方式 |
| --- | --- | --- |
| static/import | 源码可解析、入口可加载 | init.ps1 可立即运行 |
| 确定性行为 | 给定场景的状态/版本/权限/异常路径 | init.ps1 运行 `tests/`（当前仅 WorkingMemory 2 个用例）；其他模块先恢复或补充测试 |
| 真实 MCP/数据库 | 连接、Schema、过滤、资源生命周期 | 使用隔离数据，单独验收，不从 mock 推导 |
| 真实模型 E2E | 有限任务的交付与成本 | 先固定任务、标准、调用/时间上限和可发送材料；失败也保留 |

单独运行：`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp <新的 TEMP 隔离目录>`。
无测试时不能把零收集算成功。备份和私有材料注意事项见 [清理记录](../project_cleanup.md)。

## 审查发现与边界

已确认：旧交接同时写“完成”与“正在运行”；Done 条件仍绑定旧生命周期任务；功能表“仅一个生产 Runner”落后于 Team 接入；缺少统一 Claude 入口和 docs 导航。
调整：缩短根指令、集中当前状态、保留历史、增加只读入口。没有更改业务源码或产品行为。
风险：外部 TEMP 备份不是长期存储；缺少仓库内测试削弱后续回归；规范需要后续会话维护。

## 方法来源

采用课程的仓库记录、渐进披露、明确范围、证据门禁和会话交接原则。Windows 使用 PowerShell，不为评分复制 Bash 入口。

- [Repository as System of Record](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-03-why-the-repository-must-become-the-system-of-record/)
- [Split Instructions Across Files](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-04-why-one-giant-instruction-file-fails/)
- [Feature Lists](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-08-why-feature-lists-are-harness-primitives/)
- [Evidence before Completion](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-09-why-agents-declare-victory-too-early/)
- [Clean Handoff](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-12-why-every-session-must-leave-a-clean-state/)
- [Continuity / Decisions](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-05-why-long-running-tasks-lose-continuity/)
- [Initialization](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-06-why-initialization-needs-its-own-phase/)
- [Observability](https://walkinglabs.github.io/learn-harness-engineering/en/lectures/lecture-11-why-observability-belongs-inside-the-harness/)
- [Templates](https://walkinglabs.github.io/learn-harness-engineering/en/resources/templates/)

结构评分不能证明任务成功率、安全性或应用性能。保留测试缺失等真实限制，不以 100 分替代验收。
