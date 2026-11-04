# Project Harness and Claude Handoff Implementation Plan

> **For agentic workers:** Use `executing-plans` to carry out this documentation-only task inline. No business-code implementation, test-suite restoration or Git commit is authorized by this plan.

**Goal:** 依据 Learn Harness Engineering 整理可持续使用的项目管理入口，汇总现有 docs 为 Claude 交接文档。

**Architecture:** 根目录仅放短指令、机器可读功能状态和当前会话状态；交接文档说明系统事实与证据边界；主题原文按需阅读。历史状态先存档，原有 docs 不覆盖。

**Tech Stack:** Markdown、JSON、Windows PowerShell、现有 Python 环境。

**Spec:** 用户本轮请求及 https://walkinglabs.github.io/learn-harness-engineering/en/ 。用户“总体目标是”后尚缺补充；本计划只落实已明确的三个交付项，不推断新增产品功能。

## Global Constraints

- 不修改 app/、.env、默认模型、依赖、数据库、模型文件或 Git 历史。
- 保留全部已有 docs 和未提交源码；不恢复用户刚删除的测试代码。
- 不调用模型/API，不自动提交或推送；不通过恢复原始实验记录对外发送私有材料。
- 历史回归、真实模型场景、结构评分及当前健康检查必须分别标注。

## Review Focus

- 旧报告中的“未实现/运行中”不得覆盖较新源码或最终报告。
- 冻结提交不等于当前脏工作区；待完善的长期记忆注入不能算成已验收。
- 原始历史恢复不等于跨进程恢复执行；Team 不保证通用 exactly-once。
- 测试已删除，任何入口不能把缺少测试算作全回归通过。
- 原始实验在外部 TEMP 备份，不能将文档引用冒充仍存在的工作区文件。

## Tasks and Acceptance

- [x] 盘点根目录状态、docs、核心装配接口和课程；记录结构审查基线。
- [x] 存档旧 AGENTS/feature/progress/handoff；保存源码与原文哈希用于保护校验。
- [x] 汇总 docs/PROJECT_HANDOVER.md 和 docs/DOCUMENT_INDEX.md，覆盖已有主题与实施计划。
- [x] 修订 AGENTS.md，新增薄 CLAUDE.md 入口；建立简洁管理规范和只读健康检查入口。
- [x] 整理 feature_list.json、progress.md、session-handoff.md，区分历史证据、当前限制和未授权待办。
- [x] 执行只读入口、源码/文档保护校验、链接与状态一致性检查；记录实际结果。

验收：新会话只读仓库即可回答“系统是什么、如何组织、如何运行、如何验证、当前到哪/下一步是什么”；不需要靠聊天历史猜测。结构检查分数仅说明文件组织，不证明应用可靠性。
