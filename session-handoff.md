# Current Session Handoff — 2026-11-04（capability-phase1-001）

格式按课程模板：Currently verified / Changes / Still broken or unverified / Next best action / Commands。

## 当前任务 / Scope

**最新（2026-11-05）：route-auto-001**（用户“先按如此写”）。新增 `--execution-mode auto`：每个新任务在计划前选一次模式，先宿主规则（只有允许写文件且开沙盒时团队才是候选），再用一次不带工具的模型判断，任何异常退回普通模式；被阻塞任务恢复时沿用原选择。决策 D-026，详见 [按任务动态选择执行模式](docs/harness/route-auto-2026-11-05.md)。已提交 `b20282b`。随后新增只读问答任务 `billing_qa`（bench-qa-001，**未提交**）并真实评测 6 次：路由 6/6 正确；parse_duration 3/3 严格通过；billing_qa 0/3，原因是普通模式把所有步骤结果拼成最终答案（`runtime.py`），另有 1 次在只读任务里写了文件。随后对照评测 6 次：拼接答案 0/6，只取最后一步 5/6，但 4/6 次改动了工作区（完整严格通过 1/6）。用户随后决定“两项一起改”（answer-readonly-001，D-027，**未提交**）：普通模式最终答案只取最后一步；auto 路由同时判断 `writes`，为 `false` 时宿主收回写入和运行工具。确定性测试通过，历史回归新增 1 项预期失败（旧测试断言拼接答案）。真实 6 次：严格通过 3/6，工作区全部未改动；遗留 2 个问题：没有运行工具时数值算错（2 次），模型直接输出答案 JSON 导致 `StepFormatError`（1 次）。随后新增只读运行工具（readonly-run-001，D-028，**未提交**）：沙盒的 `run_python_code` 把工作区挂成只读，只交给只读任务。真实 6 次严格通过 6/6，工作区全部未改动；格式混淆问题未修改，只是这次没出现。第二阶段（按步骤选择）未做。

**已完成：capability-phase1-001**（用户 2026-11-04 授权“先按照这个开始”：增强路线的阶段 1）。

```text
Feature: capability-phase1-001
Scope: app/agent/tool_calling_model.py、openai_compatible_tool_calling_model.py、team/session.py、team/diagnostics.py、
       tool_calling_step_runner.py（只加恢复逻辑；用户未提交的中文 docstring 原样保留，提交时只提交本轮 hunk）；新增 bench/ 和 tests/
Exclusions: 不改 .env、模型默认值、依赖；不开放写权限（属于阶段 2）；不推送
Verification: tests/ 新增确定性测试；历史 Team/路由/适配器测试（git 9138ac1 → TEMP）回归；init.ps1
Authorization: 代码修改 + 确定性测试；评测集的真实模型运行须另行授权并设调用上限
Done when: 参数错误可有界修复、错误链可见、评测集与评分器有测试；回归通过；TEMP 与 __pycache__ 已清理
```

**结果（2026-11-04）**：确定性验证通过（新增测试 25 个；历史 tests/agent 450 passed）。第一轮真实评测（6 次，已授权）**严格交付 0/6**：single 三项都 completed，代码基本正确（宽松诊断 16/16、20/21、11/11，浏览器实测 9/9），但答案前后夹带说明文字；team 三项都失败，失败类型因运行器 bug 丢失（已修复）。详见 [阶段 1 记录](docs/harness/capability-phase1-2026-11-04.md)。active_feature=null，**尚未提交**。

**已完成（2026-11-04）：team-v2-001**（用户“提交基线后开工”）。V1 基线已提交为 `480f6fb`。团队模式 V2 已实现：负责人只用 `delegate`（可选 `review`）委派，每次都是全新上下文的普通模式执行器，开启沙盒时由宿主判定完成；外层计划只产生一步；`--team-version 2` 选择，默认仍为 1。确定性测试和历史回归通过，**V2 改动未提交**。第一次真实评测（授权 2 次）0/2：都在规划阶段失败，没进入团队；已改为固定单步计划并补端到端测试。第二次（再授权 2 次）0/2：负责人第一次请求被拒，原因是 JSON 模式要求消息含 “json” 且只能输出 JSON；已改为 JSON 结果格式。第三次（再授权 2 次）**2/2 严格通过**（parse_duration 132 秒、fix_inventory 39 秒），详见 [V2 实现记录](docs/harness/team-v2-2026-11-04.md)。V2 已提交 `29c2efe`；默认仍是 V1。稳定性对照（授权 12 次）：V2 6/6，普通模式 4/6；V2 弱点是执行者上下文超窗后靠重新委派兜底。执行者超窗已修复（team-v2-context-001，未提交）：真实 3 次严格 2/3，超窗 1/3，剩余缺口已补未重跑。验收门槛已改为独立验收测试（team-v2-acceptance-001、D-020，未提交）：真实 3 次严格 2/3，没再接受错误答案，但验收者失败导致一次正确交付被判 blocked，成本约 2–4 倍。已加固（team-v2-acceptance-002、D-021，未提交）；真实 3 次严格 1/3，验收测试常写错并拦下正确交付。汇总：自测门槛 6/7，独立验收 3/6。已按用户决定改为默认关闭（team-v2-acceptance-003、D-022），`--team-acceptance` 可选开启。这四轮改动已提交为 `51d638f`。2026-11-05（用户“按你所说优化”）：宿主自己记录执行者的工具结果，最终报告格式出错不再丢掉已测通的交付（team-v2-evidence-001、D-023）；命令行和评测器默认改为 V2，`--team-version 1` 仍可选（team-v2-default-001、D-024）；这两项**未提交**。真实 3 次严格 2/3（一次交付违反“只用 re”，评测器已补记 `guard_reason`）；本批未出现格式错误，D-023 主路径未在真实运行中触发。之后按用户决定删除了 V1 和早期原型（team-v1-removal-001、D-025，**未提交**）：团队模式只剩 V2，`--team-version` 已移除；历史回归基线变为 1022 passed，另有 4 个 V1/原型测试文件无法收集。

**已完成（2026-11-04）：team-recovery-001**（用户要求“先做第 1、2 项”）。成员只因协议错误失败时交回负责人，由它新建替补成员（团队最多 2 次）；修复次数按每次运行重置（团队合计最多 8 次）。决策 D-018，详见 [团队恢复能力](docs/harness/team-recovery-2026-11-04.md)。确定性测试和历史回归通过，**未提交**。真实重跑 2 次（已授权）：0/2，但执行成员都完成了写和测并交出候选，之后都在建审查成员的宿主检查上终止；评测器失败信息缺陷已修复。审查成员工具检查已改为可恢复的拒绝；再次重跑 2 次，两次都在负责人第一次调用就因调用未注册的工具（KeyError）失败，已复现。未注册工具名已改为有上限的修复；再次重跑：parse_duration 第一次走到审查通过，随后团队工具预算 32/32 用完；fix_inventory 评测器 600 秒超时。沙盒团队预算已调高；再次重跑：两次都因会话上下文超出 32768 窗口失败，负责人审查通过后也从不结束任务。已实现视图压缩和 finish_ready；再次重跑：两项都在 700 秒被评测器终止且没有轨迹（成功轨迹是 INFO 级别，团队时限按步骤计算），已补诊断。下一步：经授权用 `--timeout 1500` 再跑。

**已完成（2026-11-04）：team-diagnosis-001**（用户授权 2 次团队重跑）。结论：两次都是**工作成员**在第一次运行内连续 3 次产生无法解码的工具参数，2 次修复用完后抛出，而成员失败会直接终结整个团队；负责人修复次数为 0/2 和 2/2。出错调用很可能是内嵌完整代码的 `submit_result`（推断，轨迹已补记工具名，需再跑确认）。修复建议未实施，等用户决定。详见 [团队模式失败诊断](docs/harness/team-diagnosis-2026-11-04.md)。轨迹相关改动**未提交**。

**最新（2026-11-04）**：第四步真实评测完成：**普通模式 2/2 严格通过**，团队模式 0/2，详见 [沙盒真实评测](docs/harness/sandbox-live-2026-11-04.md)。第三、四步的改动**未提交**。下一个最值得做的是团队参数错误和失败诊断（待办 team-failure-diagnosis-001）。

沙盒第三步 `sandbox-step3-001` 已完成（运行工具已接入两种模式；团队必须先有通过的运行才能结束任务；决策 D-017），全量回归 1160 passed、14 skipped，**未提交**。下一步是第四步：真实评测，需要用户授权。建议命令：

```powershell
.venv/Scripts/python.exe -m bench.run_bench --confirm-live --sandbox --tasks parse_duration fix_inventory --modes single team --runs 1 --team-timeout 400 --out output/bench-report.json
```

沙盒第二步 `sandbox-step2-001` 已完成：镜像 `aletheia-sandbox:py313` 已构建，执行器 `app/tools/sandbox/executor.py` 已实现，10 项真实容器破坏性测试全部通过（决策 D-016）；全量回归 1146 passed、13 skipped。第一、二步都**未提交**。下一步是第三步（接入运行工具）。真实容器测试命令：

```powershell
$env:ALETHEIA_DOCKER_TESTS='1'; .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_sandbox_docker.py --basetemp <新的 TEMP 目录>
```

沙盒第一步 `sandbox-step1-001` 已完成（受限写入、审查成员不得获得写入/运行工具、评测集 `--write` 文件交付；决策 D-015），确定性测试通过，**未提交**。第二步需要 Docker 引擎可用：当天进程未运行，命令行连不上 `dockerDesktopLinuxEngine`。

**新增（2026-11-04）**：[沙盒执行设计方案](docs/sandbox_execution_design.md)（草案，未实现），等待用户审阅并回答第 12 节的问题；backlog 新增 `sandbox-execution`。

下一步（backlog，均未授权）：`deliverable-format-001` 让最终答案只含交付物；`team-failure-diagnosis-001` 只跑 team 三项（3 次）以拿到失败类型。

```powershell
.venv/Scripts/python.exe -m bench.run_bench            # dry run
.venv/Scripts/python.exe -m bench.run_bench --confirm-live --modes team --runs 1 --out output/bench-report.json   # 需授权
```

提交时注意：`app/agent/tool_calling_step_runner.py` 里混有用户未提交的中文 docstring，只能提交本轮 hunk（导入、`argument_repair_count`、`_generate` 消息、主循环 try/except）。

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

- **Team 交付质量没有改善**：本轮只做核对；真实任务严格交付仍是 Team 0/3。
- **工作区仍有用户差异**：长期记忆源码（未验收）、README、旧 tests/scripts 删除项，记录在 [基线清单](docs/harness/history/baseline-2026-11-04.json) 中。
- 外部备份目录 `C:\Users\48174\AppData\Local\Temp\Aletheia-cleanup-jjfezytd` 仍在，但本会话读取 `removed.zip` 被拒；已跟踪的旧测试可直接从 git 历史取出（D-011）。
- TEMP 里还有之前清理留下的 `aletheia-cleanup-check-*`（无权读取）和 `aletheia-cleanup-current.txt`（指向备份目录），没有处理。
- 2026-11-05 整洁性检查：`tmp/pytest-harness-*` 10 个残留目录已删除（原因是路径过长，不是权限），`tmp/` 现在为空；仓库没有未跟踪文件。
- D-028（readonly-run-001）和本次整洁性检查（tidy-001）已按用户“提交”提交；用户自己的源码、README 和旧测试删除项仍未提交。

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
