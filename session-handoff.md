# Current Session Handoff — 2026-11-04（harness-002）

格式按课程模板：Currently verified / Changes / Still broken or unverified / Next best action / Commands。

## 当前任务 / Scope

用户要求：对照 [Learn Harness Engineering](https://walkinglabs.github.io/learn-harness-engineering/en/) 检查项目能否与 Claude 交接，并按课程补全不合规的文档。
范围：只改管理文档和 init.ps1，并按用户授权新增最小测试、在本地提交 harness 文件；不改 `app/`、原始专题报告、配置、依赖；不恢复旧测试；不调用模型/DB/MCP；不推送。
状态：harness-002 已完成，active_feature=null，等待用户指定下一项任务。

## Currently verified

- `init.ps1` 通过：91 个 app Python 文件语法通过，CLI `--help` 正常，新增管理文件齐全；pytest 套件 `tests/test_harness_smoke.py` 2 passed（plan → commit → 完成；已消费 token 被拒且状态不变）。
- [基线](docs/harness/baseline.md)：已提交的 harness 文件 + 用户差异哈希清单；核对结果 base ancestor OK / drift none / uncommitted-outside-manifest none。
- HEAD 是本轮的 harness 提交（`docs(harness): ...`），父提交 `9138ac1` 为 Agent Team V1 冻结点；未推送，远端同步状态未核实。
- 受保护文件（app 源码、原始 docs、README、.env.example、pyproject.toml）哈希与本轮开始时一致，详见 [合规审查](docs/harness/conformance-audit-2026-11-04.md)。
- 历史事实（未重新测量）：真实对照中普通模式 2/3、Team 0/3；清理前 1093 passed / 2 skipped。

## Changes this session

- 新增 [合规审查](docs/harness/conformance-audit-2026-11-04.md)：逐项对照课程，给出结论与缺口。
- 新增 [DECISIONS.md](docs/DECISIONS.md)、[clean-state 清单](docs/harness/clean-state-checklist.md)、[evaluator-rubric](docs/harness/evaluator-rubric.md)、[质量文档](docs/harness/quality-document.md)。
- 按模板重排 [progress.md](progress.md) 和本文件；更新 AGENTS.md 的启动/收尾步骤、Harness 手册（状态映射、任务契约）、文档索引。
- 新增最小测试 `tests/test_harness_smoke.py`（用户授权）；init.ps1 增加管理文件存在性检查，运行 pytest，并打印标准启动/验证路径（启动命令只打印不运行）。
- 按用户授权把 harness 管理文件和 docs 在本地提交（不推送）；用户的源码改动、README 和删除项按用户选择留在工作区。
- 新增 [baseline.md](docs/harness/baseline.md) 和 [基线清单](docs/harness/history/baseline-2026-11-04.json)。
- feature_list.json：新增 harness-002。

## Still broken or unverified

- **覆盖面很窄**：只有 2 个 WorkingMemory 测试；原 1093 个测试仍在不可访问的备份中。行为改动前必须先恢复对应测试，或补充测试。
- **工作区仍有用户差异**：长期记忆接入源码（未验收）、README、旧 tests/scripts 删除项，按用户选择暂不提交，记录在 [基线清单](docs/harness/history/baseline-2026-11-04.json) 中。剩余批次：② 源码（先验收）；③ 删除项。
- 外部备份 `C:\Users\48174\AppData\Local\Temp\Aletheia-cleanup-jjfezytd\removed.zip`：本会话访问被拒（Access denied），**无法确认是否还在**；TEMP 不是长期存储。
- `tmp/pytest-harness-*` 有 10 个权限残留目录（已被 gitignore），没有改 ACL。
- 长期记忆接入（longterm-integration-001）只是部分完成，未验收。

## Next best action

1. 启动时运行 init.ps1 和基线核对命令（[baseline.md](docs/harness/baseline.md)），确认工作区与交接时一致。可选（需用户授权）：提交剩余批次；把备份移到长期存储后恢复旧 tests。
2. 之后按 backlog 优先级：team-delivery-reliability → longterm-acceptance。开工前先拆成单会话可完成的条目，并写好任务契约（手册模板）。

**不要碰**：工作区里用户未提交的 `app/` 改动；原始 docs 报告的正文（只在索引/总交接中纠正）；`.env`、模型默认值、依赖；冻结提交（不 amend、不重复提交）；任何旧的付费验证脚本。

## Commands

```powershell
# startup / 健康检查（只读）
git status --short; git log -1 --oneline
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
# verification（init.ps1 已包含；单独运行时 basetemp 用新的 TEMP 目录）
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp $env:TEMP/aletheia-pytest-UNIQUE
# run（需真实模型/MCP，须获本轮授权）
.venv/Scripts/python.exe -m app.agent 'Read README.md and summarize it' --filesystem-root . --no-knowledge
# debug：CLI 参数与导入
.venv/Scripts/python.exe -B -m app.agent --help
```

上一轮交接（harness-001）的内容已归入 progress.md 的会话记录；更早的管理状态见 [历史存档](docs/harness/history/2026-11-04-state-before.json)。
