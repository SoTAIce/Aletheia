# Current Session Handoff — 2026-11-04（team-audit-001）

格式按课程模板：Currently verified / Changes / Still broken or unverified / Next best action / Commands。

## 当前任务 / Scope

**已完成：frontend-compare-001**（用户 2026-11-04 授权：用 single 和 team 模式各跑一次真实模型的复杂代码任务——设计一个简单前端页面）。结果：**两种模式都失败**（single 为 StepModelError，team 为 JSONDecodeError），都没有产出 HTML。详见 [对照记录](docs/harness/frontend-compare-2026-11-04.md)。active_feature=null。本轮改动未提交。

```text
Feature: frontend-compare-001
Scope: 同一份 spec.md（单文件 HTML 待办页面）放在 TEMP 沙箱根目录；两种模式各跑 1 次
Exclusions: 不改 app/、.env、模型默认值；不开 write-root（两种模式都只读，HTML 作为最终答案输出）；web search 关闭（进程内 TAVILY_API_KEY 置空）；不重试
Verification: 答案存为 HTML，在浏览器中逐项检查 spec 的 7 条功能要求 + 无外部资源 + 无控制台错误
Authorization: 本轮用户授权真实模型调用（.env 中的 qwen3.8-flash）；上限沿用默认：max-executions 16、max-replans 2、Team 48 次模型调用/180s
Done when: 两次运行的状态、次数、耗时和逐项验收结果已记录；TEMP 目录和仓库 __pycache__ 已删除
```

上一项 team-audit-001 已完成（提交 cf32262），契约如下：

```text
Feature: team-audit-001
Scope: app/agent/agent_team_runner.py, app/agent/team/*; 对照 docs/agent_team_v1.md、PROJECT_HANDOVER §7
Exclusions: 不改 app/ 源码；不调用模型/MCP/DB；不恢复旧 tests 到仓库；不推送
Verification: 旧 Team 测试（取自 git 9138ac1）在 TEMP 中运行；新增文档契约测试进入 tests/ 并由 init.ps1 运行
Authorization: 只读源码 + 确定性测试；提交需另行确认
Done when: 每条文档声明都有 一致/不一致 + 文件:行号 证据；TEMP 产物已删除；状态文件已更新
```

上一项 harness-002 已完成（提交 708a70f）。

## Currently verified

- Team 结论：16 条文档声明中 15 条与代码一致，1 条表述不精确（修订上限）已更正；没有发现代码缺陷。详见 [Team 一致性审查](docs/harness/team-audit-2026-11-04.md)。
- 历史 Team 测试（git `9138ac1`）**68 passed**、路由测试 **42 passed**：在 TEMP 隔离目录中针对当前工作区代码运行，目录已删除并确认不存在。
- `init.ps1` 通过：91 个源码文件语法、CLI help、管理文件、pytest **10 passed**（WorkingMemory 2 个 + Team 契约 8 个），basetemp 自动删除。
- HEAD `708a70f`（harness 提交），父提交 `9138ac1` 为 Team V1 冻结点；未推送。
- 历史事实（未重新测量）：真实对照中普通模式 2/3、Team 0/3。

## Changes this session

- 新增 [tests/test_team_contracts.py](tests/test_team_contracts.py)：固化默认上限、只读工具、宿主契约、2000 字反馈上限、未知命令拒绝、12 次修订、所有权守卫、工具交集。
- 新增 [Team 一致性审查](docs/harness/team-audit-2026-11-04.md)；更正 [PROJECT_HANDOVER §7](docs/PROJECT_HANDOVER.md) 的修订上限表述，并补充 4 处未写明的细节。
- DECISIONS 新增 D-011（历史测试从 git 取到 TEMP 运行，测完删除）；clean-state 清单补充 TEMP 清理要求；质量文档把 Team 的测试稳定性 D→C、可读性 B→A。
- init.ps1：pytest 结束后用 `\\?\` 前缀自动删除 basetemp（可处理超长路径）。
- feature_list.json：新增 team-audit-001（done）。

## Still broken or unverified

- **本轮改动未提交**（等待用户确认）：tests/test_team_contracts.py、init.ps1 和上面列出的文档。
- **Team 交付质量没有改善**：本轮只做核对；真实任务严格交付仍是 Team 0/3。
- **工作区仍有用户差异**：长期记忆源码（未验收）、README、旧 tests/scripts 删除项，记录在 [基线清单](docs/harness/history/baseline-2026-11-04.json) 中。
- 外部备份目录 `C:\Users\48174\AppData\Local\Temp\Aletheia-cleanup-jjfezytd` 仍在，但本会话读取 `removed.zip` 被拒；已跟踪的旧测试可直接从 git 历史取出（D-011）。
- TEMP 里还有之前清理留下的 `aletheia-cleanup-check-*` 和 `aletheia-cleanup-current.txt`，不是本轮产生的，没有处理。
- `tmp/pytest-harness-*` 有 10 个权限残留目录（已被 gitignore）。

## Next best action

1. 由用户决定是否提交本轮改动（只涉及 tests/、init.ps1 和 harness 文档）。
2. 如果授权 `team-delivery-reliability`：先按 D-011 用 68 个历史 Team 测试和 8 个契约测试做回归保护，再拆成单会话可完成的条目（例如先只处理 Reviewer 反馈超长导致修复耗尽的问题），并写好任务契约。

**不要碰**：用户未提交的 `app/` 改动；原始 docs 报告正文；`.env`、模型默认值、依赖；冻结提交；旧的付费验证脚本（`scripts/validate_agent_team_live.py` 会调用真实模型）。

## Commands

```powershell
# startup / 健康检查（含 pytest，basetemp 自动清理）
git status --short; git log -1 --oneline
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
# 历史 Team 测试（D-011；测完删除 $run）
$run = Join-Path ([IO.Path]::GetTempPath()) ('aletheia-team-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory $run | Out-Null
git archive -o "$run/src.tar" 9138ac1 tests/agent; tar -x -f "$run/src.tar" -C $run
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -c pyproject.toml --rootdir $run --basetemp "$run/bt" "$run/tests/agent/test_team_v1.py" "$run/tests/agent/test_team_reliability.py" "$run/tests/agent/test_team_contract_review.py" "$run/tests/agent/test_team_session_poc.py"
Remove-Item -LiteralPath ('\\?\' + $run) -Recurse -Force; Test-Path -LiteralPath $run   # 应为 False
# run（需真实模型/MCP，须获本轮授权）
.venv/Scripts/python.exe -m app.agent 'Review the supplied files' --execution-mode team --filesystem-root . --no-knowledge
```

更早的会话记录见 progress.md；更早的管理状态见 [历史存档](docs/harness/history/2026-11-04-state-before.json)。
