# Current Session Handoff — 2026-11-06（成熟度五个阶段已完成）

格式按课程模板：Currently verified / Changes / Still broken or unverified / Next best action / Commands。

## 当前状态（2026-11-06，最新）

- **已完成并提交**（本地 `main`，未推送）：测试与安全（`55790f7`）、执行可靠性（`513608c`）、可观测性（`210436f`）、扩充评测与两项修复（`9df6e93`、`9465d7a`）、偶发测试修复（`48ec8ef`）、模型调用重试（`adf656c`）、基线刷新（`fcd0618`）、交互式命令行与归档默认开启（`3003c28`）、长期记忆接入（`9749ff0`）、阻塞直接提问和恢复直接用回答（`1fd2885`）、基线哈希更新（`2c8513c`）。决策 D-029～D-043。
- **Currently verified**：init.ps1 PASS，1600 passed、16 skipped（13 项需 `ALETHEIA_DOCKER_TESTS=1`，3 项需符号链接权限）；基线核对四项正常；调参集普通模式真实评测 6/11（修复前，阿里云模型），2026-11-06 换中转站模型并完成 D-052～D-057 后重跑 11/11（bench-tune-rerun-002），留出集首次运行 11/13（bench-holdout-001）；2026-11-07 写了 [hooks 设计稿](docs/hooks_design.md)（hooks-design-001），第 11 节用户答“都按推荐”（D-058）；第 1 步（事件、两种 hook、包装层，hooks-core-001，D-059）已提交 `fe22af0`；第 2 步（审计记录、对话令牌上限，hooks-builtin-001，D-060）已提交 `030bbd0`；第 3 步（网页人工确认，hooks-approval-001，D-061）已提交 `bd54ba8`；第 4 步真实验证（hooks-live-001）：调参集 11/11 无回退，网页允许和拒绝都符合预期，已提交 `c19aa95`；随后按用户要求写了 [架构梳理](docs/architecture_review.md)（arch-review-001），未提交；随后做第 1 阶段试验（loop-trial-001，D-062）：普通模式可选用单一循环，确定性测试通过；调参集 11/11（与规划相同），调用 44 次（52）、令牌少 19%；留出集 12/13（规划 11/13），调用 57 次（67），令牌多 3%；未提交，修复后单题重测 `todo_html`、`fix_rate_limiter` 通过，`semver_compare` 查明为评分守卫误伤并已修；第 1 阶段已提交 `38ae84a`。随后离线分析令牌（loop-todo-001，D-063）：每次请求约 1.8 万令牌来自外部，修复类多出的调用多半是 `todo`；已改提示并让评测记录逐次调用；调参集 10/11，调用 25 次（改前 44），令牌约 50 万（改前约 88 万），`fix_inventory` 被评分守卫判为禁止的导入，单独重跑通过（偶发），已提交 `6199d90`；随后留出集 12/13（与改前相同），调用 33 次（改前 57），令牌约 60 万（改前约 132 万）；随后开写文件跑调参集（loop-write-check-001）11/11，写完都会读回一遍；随后开沙盒跑调参集（loop-sandbox-check-001）10/11，`todo_html` 错在纯文本匹配 `enter_key_and_trim`，记录已提交 `25397de`；随后修阻塞恢复（loop-resume-001，D-064）：回答后循环接着原来的消息做，确定性测试通过，未用真实模型验证，已提交 `ea5783b`；随后写了 [网页可选单一循环方案](docs/loop_web_plan.md)（loop-web-plan-001），未实现，未提交。
- **Live verified（e2e-live-001）**：长期记忆在真实 PostgreSQL 16、Milvus 2.6.9 和 DashScope 嵌入上 10 项通过；交互流程 3 轮真实模型：召回记忆回答正确、阻塞后原地回答并完成（共 10 次模型调用）。验证后集合、容器、临时文件已清理，Milvus 已停回。
- **Still unverified**：修复后调参集没有整体重跑；留出集、团队模式下的新任务没跑过；模型重试没在真实故障下验证。
- **Still open**：`sales_csv_qa` 只读任务无沙盒时只能手算；长期记忆没有按用户全部删除、数据库与向量库对账、召回质量改进；失败步骤的工具结果未保留；长期记忆和压缩器调用不计量。
- **工作区用户差异**：README、三个文件的中文注释、三个脚本的删除，见下文基线清单。
- **网页接入**：[设计稿](docs/web_backend_design.md) 已写并经用户确认（web-design-001，D-044，四个问题“都按推荐”）；进度回调和会话历史（web-events-001，D-045）、后端接口（web-backend-001，D-046，`python -m app.api`）、静态页面（web-frontend-001，D-047，打开 `http://127.0.0.1:8000/`）已提交为 `3933efe`；随后按用户反馈改版界面（web-ui-refresh-001，D-048），又按用户给的参考截图改为文档式排版（web-ui-editorial-001，D-049），均已提交（`081d1f3`）。对话模型可以单独设地址、密钥和模型名（agent-model-endpoint-001，D-050，用于中转站），已提交，用户已开始用中转站试用。按用户试用反馈改进网页（web-feedback-001，D-051）：SVG 显示成图片、区分说话人、每条消息可选模式、写文件和长期记忆，已提交 `081d1f3`。长文件读取修复（long-file-reading-001，D-052）：预算随模型窗口变化、转存原文、按行读、读取与分析同步、MCP 结果去重，已提交（`fe0f9d9`）；真实重跑 2 次都失败在令牌上限：第二次读取已正常，失败在计划拆得过细和步骤工具轮数用尽。随后按用户选择改了前两项（step-wrap-up-001，D-053）：工具次数用完时先请模型收尾，问答类任务优先一步完成，已提交（`fe0f9d9`）；真实重跑 1 次成功（6 次调用、约 17.7 万令牌，修复前 24 次、51 万令牌失败）。随后记录服务商的缓存命中（cache-usage-001，D-054），已提交（`fe0f9d9`）；真实跑 1 次：中转站报告输入命中 39%（7 次调用中 3 次带该字段）。新增代码搜索工具 `search_files`（search-files-001，D-055），已提交（`fe0f9d9`）；真实对比 1 次：约 7.8 万令牌，比上次 18.9 万降 59%。缓存诊断（cache-diagnosis-001，D-056）：逐次记录每次调用的命中情况和与上一次请求开头相同的比例，新增 `python -m app.agent.cache_probe` 测试模型服务的缓存，已提交（`fe0f9d9`），真实测试 1 次：中转站对相同请求稳定命中（第 2、3 次各命中 22954/22957），但每次请求另加约 1.8 万令牌。真实任务 1 次：命中 28%，最大的收尾请求因不带工具列表完全没命中。第二阶段（prompt-cache-layout-001，D-057）：缓存路由标识、收尾保留工具列表、上下文字段重排，已提交（`fe0f9d9`）；真实对比 1 次：中转站接受新参数，一步内请求开头 100% 稳定，命中 32%，隔一次一中。
- **Next best action**：用真实模型走一遍网页流程 `web-live-001`（需授权模型调用次数）；智能体本身的改进（调参集整体重跑 11 次、留出集 13 次，均需授权）可并行。

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
- **工作区仍有用户差异**：README、三个文件的中文注释、`scripts/` 下三个脚本的删除（长期记忆源码已随 longterm-memory-001 提交），记录在 [基线清单](docs/harness/history/baseline-2026-11-06.json) 中（2026-11-06 刷新）。
- 外部备份目录 `C:\Users\48174\AppData\Local\Temp\Aletheia-cleanup-jjfezytd` 仍在，但本会话读取 `removed.zip` 被拒；已跟踪的旧测试可直接从 git 历史取出（D-011）。
- TEMP 里还有之前清理留下的 `aletheia-cleanup-check-*`（无权读取）和 `aletheia-cleanup-current.txt`（指向备份目录），没有处理。
- 2026-11-05 整洁性检查：`tmp/pytest-harness-*` 10 个残留目录已删除（原因是路径过长，不是权限），`tmp/` 现在为空；仓库没有未跟踪文件。
- D-028（readonly-run-001）和本次整洁性检查（tidy-001）已按用户“提交”提交；用户自己的源码和 README 仍未提交。随后 2026-11-06 做了四方面成熟度审阅（报告在被忽略的 `output/maturity-review/`），用户决定“先补测试和安全”：恢复了被删的历史测试（D-030），加了受保护路径和沙盒遮蔽、放宽沙盒工具超时（D-029），已按用户“提交”提交。随后完成执行可靠性阶段（reliability-001，D-031～D-033，已按用户“提交”提交）：形状错误进入修复、副作用不确定不重试、失败分类和中文说明、澄清不占次数、沙盒单步时限。随后完成可观测性阶段（observability-001，D-034，已按用户“提交”提交）：令牌计量和预算、每步工具调用记录、`--result-json` 结构化结果（评测改读它）、统一日志。随后完成评测扩充阶段（eval-expansion-001，D-035，已按用户“提交”提交）：任务扩到 24 个，分调参集 11 个和留出集 13 个，报告附置信区间；2026-11-06 经用户授权 11 次，调参集普通模式各跑 1 次：6/11 通过（54.5%，95% 区间 28%～79%）。失败 5 个：`semver_compare`、`todo_html` 撞到单次输出上限 `CONTEXT_OUTPUT_TOKENS=2048`（ModelOutputLimitError）；`config_merge_qa` 服务商返回无效响应（ModelResponseError）；`fix_rate_limiter` 代码本身全对（宽松 24/24），但答案夹了说明文字，严格判定不通过；`sales_csv_qa` 7 个键算错 2 个（手算金额）。报告在被忽略的 `output/bench-tune-single.json`。随后按用户要求把单次输出上限默认值从 2048 调到 8192（output-cap-001，D-036，已按用户“提交”提交；重测 2 次后两题都不再超限，`todo_html` 通过，`semver_compare` 代码对但答案夹了说明文字）。随后让计划最后一步按目标规定的形式交付最终答案（final-answer-form-001，D-037，已按用户“提交”提交；重测 2 次：`fix_rate_limiter` 通过，`semver_compare` 被评分守卫以 `forbidden_name` 拒绝，原因未查）。随后查明 `semver_compare` 被评分守卫误伤（`__slots__`），放宽守卫的允许名单（grader-guard-001，D-038，已按用户“提交”提交）。随后给模型调用加了有上限的偶发错误重试（model-retry-001，D-039，已按用户“提交”提交）。随后开始产品形态：交互式命令行（interactive-cli-001，D-040）和对话归档默认开启（archive-default-001，D-041），已按用户“先提交这两项”提交；随后按用户“接着你的改动做”完成长期记忆接入（longterm-memory-001，D-042，已按用户“只带长期记忆部分”提交，未连真实数据库）：装配函数、可配置召回预算和策略说明、显式写入、交互命令；用户在 `context_manager.py`、`rag_agent_service.py` 的长期记忆改动已随之提交，基线清单已刷新。`test_summary_lifecycle_e2e.py` 原有的偶发失败已查明是测试时限太紧，已修复（flaky-summary-e2e-001，已按用户“提交”提交）。

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
