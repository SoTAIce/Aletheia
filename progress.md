# Aletheia Progress

格式按课程模板 claude-progress.md。状态以 feature_list.json 和 session-handoff.md 为准；历史详情见 docs。

## Current Verified State

- **Repository root**：`D:\tries\agent`（分支 main；HEAD 是 harness 提交，父提交 `9138ac1` 为 Team V1 冻结点；工作区有用户选择暂不提交的改动）
- **Standard startup path**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`；运行 Agent 用 `.venv/Scripts/python.exe -m app.agent <goal> --filesystem-root . --no-knowledge`（需真实模型）
- **Standard verification path**：init.ps1（static/import + pytest `tests/`，当前 2 passed）；基线核对命令见 docs/harness/baseline.md。原 1093 个测试仍未恢复
- **Highest priority unfinished feature**：backlog `team-delivery-reliability`（authorized=false）；部分完成项 `longterm-integration-001`
- **Current blocker**：没有阻塞交接的项。限制：测试覆盖很窄；用户的源码/README/删除项按用户选择暂不提交（记录在基线清单中）；外部备份在本会话无法访问

## 已确认基线（历史，未重新测量）

- 本地 HEAD 9138ac1 是 Agent Team V1 冻结点；普通执行仍是默认，Team 需显式 opt-in。
- 三组真实对照：普通严格交付 2/3，Team 0/3，不能得出准确率优势结论。
- 清理前回归 1093 passed / 2 skipped；之后 tests 和验证脚本按用户要求删除。
- 长期记忆接入代码和用户注释未提交，保留在工作区；没有默认装配，也没有完整验收结论。

## Session Record

### 2026-11-04 — harness-002：课程合规审查与文档补全

- **Goal**：对照 Learn Harness Engineering 检查能否与 Claude 交接，补全不合规的文档。
- **Completed**：合规审查；新增 DECISIONS、clean-state 清单、evaluator-rubric、质量文档、已记录基线；按模板重排 progress/handoff；更新 AGENTS、手册（状态映射、任务契约）、索引；新增最小测试 `tests/test_harness_smoke.py`，init.ps1 运行 pytest 并打印启动/验证路径。
- **Verification run**：init.ps1 PASS（pytest 2 passed）；基线核对 HEAD OK / drift none；受保护文件哈希无变化；活动 Markdown 相对链接全部有效（详见合规审查和 harness-002 的 evidence）。
- **Evidence recorded**：[conformance-audit-2026-11-04.md](docs/harness/conformance-audit-2026-11-04.md)，feature_list.json → harness-002
- **Commits**：按用户授权在本地提交 harness 管理文件、docs 和最小测试（`docs(harness): ...`，见 git log），未推送
- **Known risks**：测试覆盖很窄；用户差异未提交；TEMP 备份不可访问或可能丢失
- **Next best action**：等待用户指定任务；需授权的可选项：提交剩余批次、恢复旧 tests

### 2026-11-04 — harness-001：Harness 与 Claude 交接整理

- **Goal**：按课程管理仓库，把 docs 整合成 Claude 交接材料，并修订 AGENTS.md。
- **Completed**：存档旧管理记录并给源码/文档做哈希；编写总交接与完整文档索引；新增 AGENTS.md 和薄 CLAUDE.md；整理功能状态和当前交接；新增只读 init.ps1。
- **Verification run**：init.ps1 PASS（91 个文件 + CLI）；125 个受保护文件未变；原有 31 篇 docs 全部进入索引；87 个相对链接有效；独立只读审查通过。结构启发评分 80→84/100（只反映文件组织，不代表应用可靠性）。
- **Evidence recorded**：[validation-2026-11-04.md](docs/harness/validation-2026-11-04.md)
- **Commits**：无
- **Known risks**：tests 缺失；TEMP 备份不是长期存储；10 个权限残留目录
- **Next best action**：等待用户指定任务；backlog 仅为建议

旧的进度、交接和功能表原文见 [历史存档](docs/harness/history/2026-11-04-state-before.json)，不能当作当前状态。
