# Aletheia Progress

格式按课程模板 claude-progress.md。状态以 feature_list.json 和 session-handoff.md 为准；历史详情见 docs。

## Current Verified State

- **最新验收（2026-11-09）**：kb-index-cli-003 已提交为 `64b2a49`，真实建库完成（61 个文件、844 块，失败 0，与切块结果逐条一致）；init.ps1 94 个 Python 文件、1720 项通过、16 项跳过。
- **仓库状态**（2026-11-10 更新）：D:\tries\agent，`main` 已推送到 `origin/main`（`ae5440b`）；以下为 11-09 时的记录：工作区保留用户差异和本轮记录（含 common.md 新增的 Milvus 启动一节），未提交，active_feature=null。
- **标准验收路径**：powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1；基线核对见 docs/harness/baseline.md。该检查不验证真实模型、外部服务或容器。
- **当前限制与下一步**：追踪 trace-001 已实现、未提交（D-073）；普通模式默认是单一循环（D-072，已提交 `45291d9`）；init.ps1 1756 项通过、16 项跳过；完整任务列表见 session-handoff.md 顶部。Milvus 不自启，启动命令见 common.md。

## 已确认基线（历史，未重新测量）

- 本地 HEAD 9138ac1 是 Agent Team V1 冻结点；普通执行仍是默认，Team 需显式 opt-in。
- 三组真实对照：普通严格交付 2/3，Team 0/3，不能得出准确率优势结论。
- 清理前回归 1093 passed / 2 skipped；之后 tests 和验证脚本按用户要求删除。
- 长期记忆接入代码和用户注释未提交，保留在工作区；没有默认装配，也没有完整验收结论。

## Session Record

### 2026-11-11 — stage4c-001：删除规划模式和骨架（设计）

- **Result**：4c 详细设计写进 [docs/stage4_plan.md](docs/stage4_plan.md) 第 12 节，测试清单写进第 13 节。原则是发给模型的请求和 `plan`/`step` 事件逐字不变；删除约 1,500 行骨架代码，`taskstate.py` 只留值类型；4b 对照测试改为对照删除前录下的期望值文件。
- **测试清单**：原本派给 Codex 盘点，它用完额度（11 月 15 日恢复），没有产出。改由 Claude 用本机 pytest 探针跑全部测试，记录每项测试实际构造的骨架类：997 个测试函数里，48 个文件的 348 个碰到骨架；约删除 153 个、迁移 14 个、修改 185 个。另列出 6 项目前只在旧路径上测过、要在 `LoopRun` 上补测试的宿主保证。
- **用户回答**：测试清单“可以”；联网搜索演示改写成循环；由 Claude 现在实现。
- **4c-1**：对照测试改为与 67 个期望值文件比较（`tests/fixtures/loop_run_golden/`），文件在旧路径还在时由旧路径录下；见方案第 14 节。
- **4c-2**：测试换到循环路径（方案第 14 节）：新增 `tests/loop_fakes.py`、`tests/test_loop_run_guarantees.py`（12 项），服务层库默认值改为 `loop`，联网搜索演示改用 `LoopRun`；全部测试 2093 通过、16 跳过；仍碰到骨架的测试函数从 348 降到 174，基本都是 4c-3 要删的。发现循环的令牌预算只在一步前后检查，已记录，未改。
- **4c-3**：用户确认三份笔记已保存后删除骨架（方案第 14 节）：6 个模块从工作区删除（先核对哈希，没有暂存），`runtime.py`、`taskstate.py` 缩小，服务层、组装器、命令行、网页、评测只收 `loop`；删除 15 个测试文件和清单里的部分测试，期望值对照 67 项仍全部通过，新增 `tests/test_stage4_cleanup.py`；`init.ps1` 通过，1731 通过、16 跳过；`app/` 相对 `70f7470` 新增 185 行、删除 2,861 行。
- **4c-4**：`AGENTS.md` 两条不变量改写、新增 D-076，`CLAUDE.md` 架构概览、`docs/agent_runtime.md`、总交接、README、文档索引按现状改写，基线清单刷新为 `baseline-2026-11-11-stage4c.json`（方案第 14 节）。
- **评测**：调参集单一循环 9/11、团队 10/11，留出集单一循环 6/13（低于 10/13）。留出集 7 道失败里 6 道处在服务商的少开销状态，4 道代码题的 `_load` 失败都在其中（方案第 14 节“4c 评测”）。
- **Not done**：未提交；用户决定今天先停在 4c，留出集是否在服务商状态正常时重跑另定。

### 2026-11-11 — stage4b-001：循环脱离规划—执行骨架

- **Result**：`RunState`/`RunMemory`（Codex）、`LoopRun` 加 67 项新旧路径对照测试（Codex，Claude 审查）、服务层与 `RunAssembler` 接线（Claude）；单一循环和团队任务不再经过 `Planner`/`Executor`/`StepExecutor`/`TaskState`。记录见 [docs/stage4_plan.md](docs/stage4_plan.md) 第 10 节。
- **Verification**：`init.ps1` PASS，2084 通过、16 跳过；4 项现有接线测试的替身改为顶替 `LoopRun`。
- **Live bench**（用户“跑评测”）：单一循环 9/11、团队 9/11（3b 为 10/11、11/11），令牌和调用不升。多出的失败 4b 之前的按需委派评测也出现过。重跑两题（用户“重跑两题”，各模式 1 次）：单一循环两题都过，团队 `config_merge_qa` 过、`todo_html` 仍错在 `enter_key_and_trim`；合并为单一循环 11/11、团队 10/11。重跑时 `config_merge_qa` 首个请求开销更低却通过，原先“失败因提示开销缺失”的判断已在方案 11.1 更正，改为这两项检查本身不稳定。
- **Not done**：4c、4d 未开始。已按用户“提交 4b”提交，未推送。

### 2026-11-11 — stage4a-001：拆服务层

- **Result**：用户“都按建议”确认第 4 阶段方案。`RunAssembler`（Codex 完成后又卡住）加上 `query_result` 拆成 8 个私有方法（Claude），276 行降到 50 行，行为不变；记录见 [docs/stage4_plan.md](docs/stage4_plan.md) 第 8 节。
- **Verification**：现有测试未改全部通过；新增 `tests/test_run_assembler.py` 6 项；`init.ps1` PASS，1833 通过、16 跳过。用户四个文件 sha256 前后一致。
- **Committed**：`e6a9856`。
- **4b 设计**：方案第 9 节（stage4b-001，planned）：`RunState`/`RunMemory`、`LoopRun`、结果对照表、新旧路径逐项对照测试、分 4b-1/4b-2/4b-3 三块实现。

### 2026-11-11 — stage4-plan-001：第 4 阶段方案

- **Result**：按需委派已提交 `a0aa489`（用户“那提交吧”）。第 4 阶段方案写成 [docs/stage4_plan.md](docs/stage4_plan.md)：4a 拆服务层、4b 循环脱离骨架、4c 删除规划模式和骨架、4d 模型档案推迟。
- **Not done**：等用户回答第 7 节五问；未改代码，未提交方案。

### 2026-11-11 — loop-delegate-001：单一循环按需委派（实现）

- **Result**：Codex 71 分钟无输出、未改文件，已取消，由 Claude 实现方案第 5、6 节；记录见 [docs/loop_delegate_plan.md](docs/loop_delegate_plan.md) 第 10 节。新增 `--delegate`（默认关）、`ExploreDelegation`/`MemberSpawner`/`ExploreLoopRunner`、宿主工具按轮次提供、评测委派计数、两道 `probe` 探针题。
- **Verification**：`init.ps1` PASS，1827 通过、16 跳过；新增 `tests/test_loop_delegate.py` 15 项。
- **Live bench**（用户“跑评测”）：调参集开委派 10/11，511,221 令牌（3b 为 606,738），委派 0 次；探针题开、关都 6/6，但 `probe_config_keys` 开委派时委派 2 次，令牌 897,740，不开时只要 145,401。结论见方案第 11 节：默认保持关闭。
- **Committed**：`a0aa489`。

### 2026-11-10 — loop-delegate-plan-001：单一循环按需委派方案

- **Result**：用户认为团队应由智能体自己判断是否委派，要求参考 Claude Code 写方案并把握触发时机。查了 dsh（`subagent_spawn` 工具、团队插件）和 Claude Code（`Agent` 工具、Explore 等内置子智能体、全新上下文、嵌套与工具限制、不该用的情况），写成 [docs/loop_delegate_plan.md](docs/loop_delegate_plan.md)。
- **Not done**：未实现；没有改代码，没有真实模型运行；未提交。

### 2026-11-10 — stage3-plan-001：第 3 阶段方案与团队基线

- **方案**：[docs/stage3_team_delegate_plan.md](docs/stage3_team_delegate_plan.md)；用户“都按建议”回答第 9 节五问。
- **团队基线（用户授权，真实模型）**：调参集团队模式、写文件加沙盒各 1 次，7/11，95 次调用，2,002,917 令牌，806 秒；报告 `output/bench-tune-team-baseline.json`，追踪 `output/traces-team-baseline/`。三道问答题都因完成条件在没有写入时也要求一次通过的运行而失败（旧实现缺陷），`todo_html` 错在 `enter_key_and_trim`。方案第 3.3 节据此改为“有写入才要求通过的运行”，判断标准改为代码类 7 题不低于 6/7、总数不低于 7/11（第 10.1 节）。
- **下一步**：派 Codex 实现 3a。未提交。

### 2026-11-10 — stage3-team-loop-001（3a）：实现与审查

- **Result**：Codex 按方案实现 3a（见方案 9.1 节）；Claude 审查：范围符合，用户四个文件差异行数不变，`team_v2.py` 未改；改正负责人提示里一句不成立的话并补测试。
- **验证**：init.ps1 1851 项通过、16 项跳过（Codex 沙箱里 1 项因读不了 `USERPROFILE` 失败，本机重跑通过）。没有真实模型运行。
- **下一步**：经用户授权跑 3a 评测：调参集团队模式 `--team-impl loop --write --sandbox` 1 次、单一循环调参集 1 次。未提交。
- **3b（用户“提交并做 3b”）**：3a 提交为 `cefcd2d`。3b 由 Codex 做到一半时额度用完，Claude 完成余下部分并撤回 Codex 对用户文件 `step_executor.py`、`tool_calling_step_runner.py` 的改动（四个用户文件与基线哈希一致）；取消与失效保护移到 `runtime._BoundRunner`；详见方案 9.2、D-075。init.ps1 1807 项通过、16 项跳过；没有真实模型运行；未提交。
- **3b Live（用户“跑评测”）**：团队调参集 11/11，78 次调用、1,837,245 令牌、674 秒；单一循环 10/11（`todo_html` 的 `enter_key_and_trim`，已知会波动），28 次调用；`config_merge_qa` 两边都通过。不低于 3a，第 3 阶段达标（方案 10.3）。用户“改并提交”：改写 `AGENTS.md` 两条不变量，提交 `bab704a`，未推送。
- **Live（用户“跑评测”）**：团队新实现 9/11（代码类 6/7 同基线，问答类 3/4），92 次调用、1,996,491 令牌、763 秒；单一循环 10/11，26 次调用、520,667 令牌、199 秒。两者都错在 `config_merge_qa` 的 `top_keys`，单一循环此前 8 次都通过，判断为模型波动，未重跑确认。达到进入 3b 的标准（方案 10.2）。

### 2026-11-10 — status-cleanup-002：两处遗留小问题

- **Result**：用户“接着修”。`app/services/vector_store_manager.py` 的 `list_documents`（`app/tools/knowledge_tool.py` 建关键词索引时调用）改用 `query_iterator` 逐批读取，迭代器在 `finally` 里关闭；去掉按 `num_entities` 的条数核对（压缩前它仍计入已删记录，删过块后会误报；offset 加 limit 还有 16384 上限）。连接方式不变（仍是可建集合的 `connect()`）。`app/services/vector_index_service.py` 的 `index_directory` 两处 `logger.exception` 改为只记异常类型（D-033）；返回结果里的失败信息不变。
- **验证**：新增 3 项测试（逐批读取且不信实体统计、失败时关闭迭代器、目录建库日志不含异常原文和堆栈），换回旧代码时 3 项都失败。`tests/test_knowledge_cli.py` 89 项通过；init.ps1 通过：1806 项通过、16 项跳过（62 秒）。没有连接真实 Milvus。
- **未做**：建库与清理并发没有事务保证，属设计限制，不改。
- **Live（用户在自己终端运行，Milvus 2.6.9 本机）**：`list_documents` 读出 844 块、61 个来源，与建库记录一致；`--exclude harness/history --prune` 预演：63 个文件、895 块，0 个过时来源，没有删除。两项都只读、没调嵌入接口；现有块数碰不到这次修的边界，真实运行只说明没改坏。知识库落后于当前文档（未建入 2 个新文件、部分文件块数变化），是否 `--confirm` 重建待用户决定。

### 2026-11-10 — status-cleanup-001：清理小问题

- **Result**：用户“先清小问题”。`app/knowledge/__main__.py` 的 `--prune` 来源前缀先去掉末尾斜杠再补，磁盘根（`C:/`、`/`）不再变成双斜杠。嵌入服务的 `ValueError` 范围经核对不改：只有本服务自己的结果校验 `EmbeddingResponseError` 原样抛出，客户端或依赖在请求中抛的 `ValueError` 归为 `EmbeddingRequestError`，建库命令据此报“调用嵌入接口失败”，分类正确；补测试固定。`--exclude` 补 `/absolute`、`\absolute`、`/harness/history`、`\harness\history` 四个拒绝用例。功能表 `longterm-integration-001` 和待办 `longterm-acceptance` 加 disposition，说明已被 longterm-memory-001 与 e2e-live-001 取代。
- **验证**：`tests/test_knowledge_cli.py` 86 项通过；把修复临时退回时新增的磁盘根测试 2 项失败，恢复后通过。init.ps1 通过：1803 项通过、16 项跳过（62 秒）。没有连接真实 Milvus 或嵌入接口。
- **工作树残留**：空文件夹 `.claude/worktrees/workspace-choice` 被进程 86428（07:04 从工作树启动的 `python -m app.api --no-knowledge`）占用；用户“结束并删除”后结束该进程，文件夹和空的 `.claude/worktrees` 已删除。未提交。

### 2026-11-10 — 收尾：删除实验工作树，网页执行记录真实确认

- **删除工作树**：先用 `cmd /c rmdir` 拆掉工作树里指向主目录 `.venv` 的目录链接，再 `git worktree remove --force`、`git branch -d exp/workspace-choice`；主目录 `.venv` 完好。空文件夹 `.claude/worktrees/workspace-choice` 被 8000 端口上一个从该工作树启动的旧网页服务（07:04 启动）占用，未删。
- **执行记录**：用户授权并亲自在网页（`main` 代码，8001 端口）用真实模型发一条消息，点开“执行记录”显示正常。审计文件经 `python -m app.agent.trace --json` 解析：版本 2，complete=true，dropped=0，状态 completed，2 次模型调用、1 次工具调用，输入 46064、输出 398 令牌，120.3 秒。
- **文档**：`common.md` 的网页启动命令改为绝对路径（D-074 起 `--filesystem-root .` 会被拒绝）。本轮未运行 init.ps1（没有改代码）。

### 2026-11-10 — workspace-001：选择工作目录、系统盘确认、禁止目录、拖拽附件（分支 `exp/workspace-choice`）

- **Result**：按方案和用户的七条回答实现（D-074）。所有目录走 `app/services/workspace.py` 的同一套检查；系统盘目录必须确认（网页对话框、`/dir` 输入 `y`、启动参数交互确认或 `--confirm-system-drive`），服务端也校验；`WORKSPACE_DENY_DIRS` 和本项目数据目录禁止读写，在工作目录里时文件工具和沙盒都看不到；网页文件夹按钮、目录对话框、拖拽 UTF-8 文本附件；命令行 `/dir`。
- **验证**：新增 `tests/test_workspace.py` 37 项，`test_run_options.py` 的启动测试固定为旧语义。init.ps1 通过，1793 项通过、16 项跳过。浏览器里用假模型和真实文件服务试过：选 D 盘目录、选 C 盘目录（确认框、Esc 取消、确认生效）、拖入文本和二进制文件、发送、手机宽度。
- **用户试用后的修改**：用户用真实模型在网页上试过，提了两条：要系统弹窗选文件夹——目录对话框加“浏览…”，服务端在本机子进程里用 tkinter 弹出系统窗口（`app/services/folder_picker.py`，只在本机地址、一次一个，返回的路径照样检查），新增 2 项测试；工作树里看不到以前的会话——不是丢失，工作树用自己的 `data`，可设 `CONVERSATION_STORAGE_DIR` 指向主目录。全部测试 1795 项通过、16 项跳过。
- **合并**：用户同意后快进并入 `main`，未推送。真实系统弹窗由用户在自己屏幕上试，自动测试用替身。

### 2026-11-10 — workspace-plan-001：选择工作目录方案（用户“可以先做第一项，输出方案”）

- **Result**：写了 [选择工作目录方案](docs/workspace_choice_plan.md)：读写共用一个工作目录（写不写仍按每条消息）；`app/services/workspace.py` 判断目录（拒绝磁盘根、网络路径、系统目录、本项目数据目录，可选白名单，标记系统盘）；服务按次生成工具服务；网页会话顶部更换目录、系统盘确认框、服务端 409 校验；命令行 `/dir`；默认关闭，非本机监听时必须配白名单。启动参数指定的系统盘目录只警告不拦截（评测临时目录在 C 盘）。
- **依据**：对照了 `tool_loader.py`、`file_access.py`、`rag_agent_service.py`、两个入口的启动代码、`app/api/app.py`、`app.js`、D-029、D-051（HEAD `db13d64`）。会改变 D-051 的一条，需要用户确认。
- **Not done**：未实现（workspace-001 为 planned）；方案第 9 节六个问题待用户回答；未提交。只改了文档和功能表（已验证可解析）。

### 2026-11-10 — trace-001：本机追踪（用户“我感觉tracing比较合理，然后你负责实现吧，codeX额度有限了”）

- **Result**：按 [追踪方案](docs/tracing_plan.md) 第一阶段实现（D-073）。`hooks.trace_scope`/`root_span` 给事件加区间编号和父子关系，作用域在 `runtime.py`（任务、规划、步骤）、`team_v2.py`（成员）、服务的路由选择；审计第 2 版；`TraceFileHook`；`trace_view.build_trace`/`render_text`（兼容第 1 版）；归档 `list_audits`/`load_audit`；服务 `task_traces`；接口 `GET /api/sessions/{session}/tasks/{task}/trace`；网页“执行记录”按钮和时间条面板；命令行 `python -m app.agent.trace`；评测 `--keep-traces`。并入服务商返回的模型名（`after_model.response_model`）。
- **验证**：新增 `tests/test_trace.py` 35 项；更新 3 个逐字比较事件的测试。init.ps1 通过，1756 项通过、16 项跳过。`node --check app.js` 通过；用假模型起本机网页，在桌面宽度和 375px 下看过面板（运行结束时和刷新后都能打开，无横向滚动）。
- **Not done**：没有跑真实模型或评测；未提交。`planner.py`、`step_executor.py`、`tool_calling_step_runner.py` 没有改。与方案的差别见方案第 10 节（按钮叫“执行记录”等）。

### 2026-11-09 — trace-plan-001：追踪方案（用户“我还希望加入tracing”“先写方案”）

- **Result**：写了 [追踪方案](docs/tracing_plan.md)：在现有 hook 事件上加区间编号和父子关系（审计文件第 2 版）、`trace_view.py` 把事件拼成树并输出文字、归档读取、服务方法、网页接口和“执行过程”面板、命令行查看、评测 `--keep-traces`。本机、不加依赖、不记原文；记录原文、服务商模型名、OpenTelemetry 导出、实时追踪列为以后另行决定。
- **依据**：对照了 `hooks.py`、`builtin_hooks.py`、`runtime.py`、`agent_loop.py`、`conversation_archive.py`、`rag_agent_service.py`、`app/api/app.py`（HEAD `45291d9`）。现有事件没有调用编号和所属步骤或成员，审计文件没有读取入口，评测不产生审计文件。
- **同时记录**：用户决定 `side_effects_qa` 和中转站疑似换上游不再处理；提出“选择工作目录、涉及 C 盘弹警告”为后续功能（未定方案）。交接里 loop-default-001 的状态改为已提交 `45291d9`。
- **Not done**：未实现（trace-001 为 planned）；方案第 9 节的问题待用户回答；本条和方案未提交。只改了文档和功能表；登记后 init.ps1 通过（管理状态检查和确定性测试，未改代码）。

### 2026-11-09 — loop-default-001：默认改用单一循环（用户“改成单一循环”“改完写好文档”）

- **改动**：`app/agent/__main__.py`、`app/api/__main__.py`、`bench/run_bench.py` 的 `--single-runner` 默认改为 `loop`；评测在 `single`、`auto` 模式下总是显式传 `--single-runner`，`team` 不传；`run_one` 默认 `loop`。`RagAgentService` 构造默认仍为 `plan`，只加一行注释（D-072 说明原因：改它会让 60 多项专测规划语义的服务测试改走循环）。
- **测试**：改 `tests/test_agent_loop.py`（评测传参覆盖 single 默认、single plan、auto、team）、`tests/test_run_options.py`、`tests/test_web_loop_runner.py`（新增入口默认与服务默认的守卫）。三个文件 67 项通过；init.ps1 PASS，94 个 Python 文件，1721 项通过、16 项跳过（58.14 秒）。
- **文档**：D-072；`common.md` 网页和评测命令改成新默认，补 `--keep-answers` 必须带目录的用法；`docs/PROJECT_HANDOVER.md` 架构图和模块表加入单一循环；`docs/DOCUMENT_INDEX.md`；AGENTS.md 与总交接测试数。
- **未验证**：改默认后没有再跑真实模型；网页选择框默认跟随 `default_runner`，没在浏览器里看。用户的 `README.md`、三个中文注释文件和三个删除项没动；未提交。

### 2026-11-09 — 改默认前的单一循环复测（用户“先再跑评测”）

- **Live**：`bench.run_bench --confirm-live --modes single --single-runner loop --runs 1`，调参集 `output/bench-tune-loop-1109.json`，留出集 `output/bench-holdout-loop-1109.json`，两次都退出码 0。
- **调参集 11/11**：25 次调用、490,917 令牌；规划模式上次（`bench-tune-plan-nodup.json`）11/11、53 次、1,149,625 令牌。
- **留出集 10/13**：37 次调用、579,136 令牌；之前循环 12/13 两次，规划模式（`bench-holdout-single-d057.json`）11/13。失败三题：
  - `failing_tests_qa`：状态 failed、没有报错信息，用时 487.3 秒，7 次调用里 5 次没有令牌数，像是模型服务那边超时或断开，上次循环通过（5/5）。
  - `fix_split_amount`：完成但答案加载失败（`_load`、`_error_type`），检查 0/0；只有 2 次调用、第一次提示仅 1,494 令牌，上次循环 25/25。没开 `--keep-answers`，看不到原答案。
  - `side_effects_qa`：`mutates_arguments` 未过，和之前每次循环、规划模式都一样。
- **判断**：每题只跑 1 次，波动大；前两题失败的原因没查清，留出集按规矩不能拿来调提示词。没有改代码，没有提交。
- **重跑留出集（用户“再跑并保留答案”）**：`output/bench-holdout-loop-1109b.json`，答案在 `output/answers-holdout-loop-1109b/`（不进版本库，用完可删）。**11/13**，32 次调用、606,471 令牌。`failing_tests_qa` 这次通过；`side_effects_qa` 仍是 `mutates_arguments`。（第一次启动漏了 `--keep-answers` 的目录参数，参数检查即退出码 2，没有调用模型。）
- **`fix_split_amount` 的原因**：保存的最终答案不是代码，而是模型把发给它的任务上下文 JSON（`task_id`、`active_goal`、`pinned_contexts`，连时间戳都一样）原样抄了一遍，所以加载失败。两次失败的第一次调用提示令牌只有 1,494 和 1,901；通过的各次（含同轮的 `fix_word_wrap`、`fix_topo_sort`）第一次都约 20,190，而系统提示和工具定义每题相同。推测这两次请求被中转站转到了另一个上游（按缓存后的令牌计数、模型更弱），未核实，报告里没有上游模型名。不是宿主拼错答案：结果来自 `result_json`，是模型自己的回复。
- **三次循环留出集**：12/13、10/13、11/13，规划模式 11/13；两题新失败分别疑似服务超时和上游异常，`side_effects_qa` 两种方式都一直不过。

### 2026-11-09 — 网页单一循环真实试用（用户操作）

- **Live（用户报告）**：Claude 给出验收清单（普通问答；写文件人工确认的允许与拒绝；`ask_user` 阻塞后接着做，见 docs/loop_web_plan.md §7）和启动命令 `python -m app.api --filesystem-root . --write-root .\output --no-knowledge --single-runner loop`；用户自己启动并在浏览器里试用，反馈“还可以，使用很流畅”。
- **未核实**：Claude 没有看到服务日志、页面或产出文件；三项是否逐项覆盖、允许与拒绝是否都试过，用户没有逐项说明。不据此声称每一项都已通过。
- **其他**：开工时 init.ps1 通过，1720 项通过、16 项跳过；没有改代码，没有提交。

### 2026-11-09 — 收尾整理（Claude）

- common.md 新增“知识库数据库（Milvus）”：启动、查看、停止命令，以及何时需要它（Milvus 没有设置自启）。
- session-handoff.md 顶部新增“新会话从这里开始”：当前状态、未提交改动、用户差异、工作方式和按优先级排列的下一步。
- init.ps1 通过，1720 项通过、16 项跳过；未提交。

### 2026-11-09 — kb-index-cli-003 审查（Claude）

- **Codex 状态**：任务在收尾阶段停下（状态 failed，10 分 39 秒），实现、测试和记录已写完，最后一次 init.ps1 已通过。
- **审查**：切块器统一再切超长块（按 UTF-8 字节，默认 8000），再切后仍超长抛 `ValueError`；`supported_files(exclude=...)` 按路径段前缀排除，拒绝盘符、单端绝对路径和 `..`；`--exclude` 可重复，非法时退出码 2，会输出排除了几个文件。都在方案范围内，没有发现需要修改的问题。
- **复跑**：建库相关 86 项测试通过；`docs/` 预演 70 个文件、984 块、退出码 0（那个基线清单 JSON 现在能切块）；`--exclude harness/history` 排除 9 个文件，剩 61 个文件、844 块；`--exclude ../x` 退出码 2；init.ps1 PASS，1720 passed、16 skipped；基线核对四项正常；用户差异未变；项目里没有缓存。
- **遗留**：系统 TEMP 里 Codex 建的空目录 `aletheia-pytest-1fd95718b5fb49ebb1c7ec6608a50c02`，权限拒绝删除，没有改它的访问权限；另一个空目录 `.tmpFjKb9r` 来源不明，没有动。
- **提交**：按用户“提交并真实建库”提交为 `64b2a49`，未推送。
- **Live**：真实建库（用户“提交并真实建库”，已提交为 64b2a49 之后）：`python -m app.knowledge docs --exclude harness/history --confirm --prune` 退出码 0；排除 9 个文件；61 个文件全部成功，写入 844 块，失败 0；清理 harness/history/ 下已建入的 7 个来源共 28 块。建库前 67 个来源 859 块，建库后 61 个来源 844 块，与现在按排除后的文件列表重新切块的结果逐条相同，没有重复或残留的旧块，没有 docs/ 以外的来源，新建入 kb_index_v3_plan.md。

### 2026-11-09 — kb-index-cli-003：实施与验收

- **目标与裁定**：按 docs/kb_index_v3_plan.md 在指定工作区实现两项改动；用户明确要求保留本工作区与既有记录，因此不另建工作树，不提交。沿用 progress.md 作为执行台账；路径首尾斜杠与绝对路径的边界见 D-071，错误解释可能导致目录内同名相对来源被排除并在显式清理时删除。
- **实现**：统一检查 UTF-8 字节数，超长块用已有文本切块器再切，复制元数据、保持顺序；仍超限在 read 阶段失败。支持可重复 --exclude、路径段前缀过滤、参数错误返回 2、实际排除计数及清理提示；其余索引与清理逻辑不变。
- **启动验证**：启动基线四项正常；init.ps1 退出码 0，94 个 Python 文件，1697 项通过、16 项跳过（54.64 秒）。
- **先失败后实现**：新测试与已有测试一起先得到 22 项失败、64 项通过；实现后 86 项通过（3.93 秒），其中新增 23 项，普通 Markdown 不变测试在实现前已通过。测试文件为 tests/test_document_splitter_limits.py、tests/test_knowledge_cli.py；使用 -B、-p no:cacheprovider 和系统 TEMP 下的 --basetemp。
- **独立只读审查**：未发现严重或重要问题；审查未独立重跑测试。延后一个轻微建议：非法值矩阵已有盘符与 UNC，尚未单列 /absolute 及单反斜杠绝对路径回归；当前实现逻辑会拒绝，未扩展本轮修复。
- **预演异常与恢复**：第一次并行导入 PyTorch 时，两次普通预演都因 WinError 1455（页面文件不足）退出 1；同入口审计成功。没有修改依赖、页面文件或系统配置，改为顺序重跑后两次普通预演均退出 0。此为资源限制，后续重型本地检查保持顺序。
- **命令**：.venv/Scripts/python.exe -B -X utf8 -m pytest -q -p no:cacheprovider --basetemp <系统 TEMP 下本轮唯一目录> tests/test_document_splitter_limits.py tests/test_knowledge_cli.py；.venv/Scripts/python.exe -B -X utf8 -m app.knowledge docs；同命令加 --exclude harness/history；powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1；基线使用 docs/harness/baseline.md 的核对命令。
- **最终预演快照**：收尾文档与清单首次刷新后，默认 docs 预演 70 个文件、984 块，原问题 JSON 104 块；加 --exclude harness/history 后排除 9 个文件，处理 61 个文件、844 块。两者均 0 个切块失败、退出码 0。用相同模块入口额外审计，最大块分别 3750、2027 UTF-8 字节，0 个超限；拦截 Milvus 连接、嵌入调用及 socket 连接/地址解析，均 0 次。该计数对应本条记录写入及最终哈希刷新前的快照，清单文本后续更新可能改变自身块数。
- **完整结果**：init.ps1 退出码 0，管理状态、94 个 Python 文件语法、CLI 加载及全套 1720 项通过、16 项跳过（53.63 秒）；比启动增加 23 项。收尾记录后再次执行同一 init.ps1，退出码 0，1720 项通过、16 项跳过（53.40 秒）；模块导入前即安装网络审计后重跑两次，计数仍为 984/844 块，服务或网络尝试均 0 次。
- **退出清单**：标准启动与全部确定性测试通过；功能表、决策、进度、交接与使用说明已同步，无未记录半成品；未新增调试或缓存产物，本轮测试临时目录已清理并复核，仓库无 __pycache__ 或 .pytest_cache（不含既有虚拟环境）。用户四个修改文件、原方案、文档索引登记及 kb2 清单保持启动哈希，三个删除项保留，索引为空；本轮清单保留开工快照及独立用户保护哈希，结束时刷新并运行基线核对。下个会话可直接按入口继续，真实验证仍待授权。
- **最终基线与保护复核**：按 docs/harness/baseline.md 原命令核对，退出码 0，输出为 base ancestor OK；drift none；deleted-restored none；uncommitted-outside-manifest none。用户四个修改文件、原方案、文档索引及 kb2 清单的启动哈希均未变，三个用户删除项仍不存在；HEAD 44d435c、索引为空，缓存扫描为空。本条补齐后再次刷新本轮清单并复核同一命令。
- **风险与下一步**：真实 Milvus、嵌入接口、真实模型和实际 --confirm/--prune 均未执行；待审查后由用户另行授权真实验证。既有磁盘根目录清理前缀、嵌入异常分类及查询日志边界问题维持历史记录，本轮不修改。


### 2026-11-09 — kb-index-cli-003-plan：超长块再切与按路径排除的方案

- **Result**：写了 [方案](docs/kb_index_v3_plan.md)，打算交给 Codex 实现（kb-index-cli-003）：切块后超过 8000 字节的块用文本切块器再切；`--exclude` 按相对路径排除，配合 `--prune` 清掉已建入的块。依据是真实建库时唯一的失败，以及离线核对：`docs/` 下只有那个基线清单 JSON 会切出超长块。
- **Not done**：未实现；未提交。

### 2026-11-09 — kb-index-cli-002 审查后修改

- **Result**：Codex 实现通过审查（先写后删、分步说明、`--prune` 都按方案）。修两处：日志静默移到 `main()` 并只用公开接口；维度不一致等集合问题改抛 `MilvusCollectionError`，命令报“Milvus 集合不可用”而不是“连接 Milvus 失败”（D-070 补记）。
- **Verification**：`tests/test_knowledge_cli.py` 63 项通过（新增 5 项）；对 `docs/` 预演 68 个文件、881 块、退出码 0，不连接任何服务；init.ps1 PASS。
- **记下未改的小问题**：嵌入服务只放过 `EmbeddingResponseError`，依赖抛的其他 `ValueError` 会被包成 `EmbeddingRequestError`；失败日志改为只记异常类型，也影响知识检索的查询路径；磁盘根目录当建库目录时 `--prune` 前缀不对。
- **已提交**：`44d435c`，未推送。
- **Live**：真实验证（用户“提交并验证吧”“跑 --confirm”，Milvus 2.6.9 本机、DashScope 嵌入）：--prune 预演只读，0 个过时来源；--confirm --prune 处理 docs/ 68 个文件，成功 67 个、写入 859 块，退出码 1。建库前 64 个来源 818 块，建库后 67 个来源 859 块；每个来源的块数都等于现在的切块结果，没有重复或残留的旧块（如 DECISIONS.md 98→104 块被干净替换），没有误删来源，新建入 3 个文件。唯一失败的 harness/history/2026-11-04-state-before.json：JSON 切块后有 3 块超过 Milvus 内容字段 8000 字符上限（最长 26491），写入被拒；先写后删使它没有影响别的数据。
- **待决定**：切块结果超过 8000 字符时没有再切（JSON 里很长的单个值切不开），应该再切或明确跳过；`docs/harness/history/` 是哈希清单，建库时建议跳过。

### 2026-11-09 — kb-index-cli-002：实施与验收

- **目标**：按 docs/kb_index_v2_plan.md 实现安全替换、失败分步说明和可选清理；同一工作区原差异和未提交方案保留，不提交。
- **启动验证**：基线四项正常；init.ps1 94 个 Python 文件、1644 项通过、16 项跳过（55.24 秒）。
- **先失败后实现**：新增测试并调整旧提示期望后，36 项失败、8 项通过；首版实现后 44 项通过（2.71 秒）。
- **边界裁定**：既有 connect 会创建集合或在显式配置下重建集合，不能用于只读预演；新增兼容的只读连接参数，列来源时禁止创建和重建。写入返回编号不完整或重复时保留旧块，不发删除请求。这是落实方案的只读与数据安全约束；代价是异常返回需要人工排查，集合不存在时预演清理报失败。六项边界测试先失败，再补实现。
- **独立审查与修复**：审查指出三项重要问题，已统一修复：来源查询不再依赖可能含已删记录的实体统计，改用只取 metadata 的 query_iterator 并始终关闭；命令全程屏蔽可能携带实体或异常原文的依赖日志，退出时恢复原状态；当前来源集合按 resolve 后路径比较，避免别名误删。六项回归在修复前全部失败；修复后最初 56 项通过、2 项测试捕获失败，按系统调试技能确认 pymilvus 日志不向根传播，将捕获器挂到依赖日志器后共 58 项通过（2.59 秒）。没有再次派审查。
- **审查裁定**：审查给出的 target.bin 符号链接示例会被现有扩展名检查拒绝，但指向排除目录内 .md 的别名确有误删风险，已用该有效场景回归。不扩展既有 index_directory 的原文日志、list_documents 分页、并发竞态或跨进程事务；这几项超出新命令与新来源枚举的范围，仍不承诺通用事务或 exactly-once。
- **延后的小问题**：磁盘根目录（例如 C:/）按方案加斜杠会得到双斜杠，--prune 可能匹配不到来源；本轮按方案保留，使用具体文档子目录，后续再扩展。
- **默认预演**：执行 `.venv/Scripts/python.exe -B -m app.knowledge docs`，68 个文件、874 块、0 个切块失败，退出码 0。另用同样模块入口加 Python 审计钩子拒绝 socket.connect/socket.getaddrinfo，预演仍成功，0 次网络连接或地址解析。该计数是更新最终记录前的文档快照。记录补齐后再次运行：68 个文件、876 块、0 个切块失败，退出码 0；同入口网络审计仍为 0 次。此第二次计数对应结束清单刷新前的快照。
- **命令**：针对性测试使用 `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp <系统 TEMP 下本轮唯一目录> tests/test_knowledge_cli.py`；完整验收 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`；基线核对使用 docs/harness/baseline.md 中的只读命令。
- **完整验证**：init.ps1 退出码 0，管理状态、94 个 Python 文件语法和命令行加载通过；全套 1692 项通过、16 项跳过（54.98 秒），比启动时增加 48 项。状态同步后再次运行用户指定的同一 init.ps1：1692 项通过、16 项跳过（55.43 秒），退出码 0。跳过项为未启用的真实容器和符号链接权限测试，不代表真实服务通过。
- **状态与授权**：kb-index-cli-002=done、active_feature=null；原方案和登记保留，用户四个修改文件与启动哈希相同，三个脚本仍删除；索引、HEAD 未改，未暂存、还原、提交、amend 或推送。没有连接真实 Milvus、嵌入或模型，没有执行真实 --confirm/--prune，没有安装、下载或改依赖、.env、模型默认值及 data/。
- **退出检查**：标准启动入口和全部确定性测试通过；功能状态、决策、说明与交接已同步，无未记录半成品；本轮测试 TEMP 已清理，仓库无 __pycache__ 或 .pytest_cache（不含虚拟环境），差异空白检查通过。用户修改文件和原方案哈希均未变，三个用户删除项保留；最终基线按下述清单核对。
- **结束清单**：docs/harness/history/baseline-2026-11-09-kb2.json 包含开工快照、独立用户保护哈希及当前全部未提交文件哈希，自身不做循环哈希；已执行基线核对：base ancestor OK；drift none；deleted-restored none；uncommitted-outside-manifest none。最终记录写完后再次刷新并复核。
- **风险与下一步**：真实服务行为未验证，待审查后由用户授权真实只读清理预演与可选确认运行；磁盘根目录前缀问题延后。不得自动继续真实验证。


### 2026-11-09 — kb-index-cli-002-plan：建库命令改进方案

- **Result**：写了 [建库命令改进方案](docs/kb_index_v2_plan.md)，打算交给 Codex 实现（kb-index-cli-002）。复查时发现 `index_single_file` 先删旧块再嵌入写入，嵌入失败会丢掉这个文件的块，方案改为先写后删；另外失败时说明是哪一步，并加 `--prune` 清掉目录里已删除文件的块。
- **Not done**：未实现；未提交。

### 2026-11-08 — tool-list-dedup-001：执行步骤不再重复发送工具清单

- **Result**：派 Codex 实现；它写完代码和测试后报用量上限，记录没写。Claude 审查：`step_models.TOOL_LIST_SOURCE` 共用常量；执行步骤序列化上下文时去掉这份清单；规划器、团队负责人和任务状态不变（D-069）。
- **用户差异**：`tool_calling_step_runner.py` 里 Codex 只改了导入和两行过滤；在内存里退掉这两处后，文件哈希和用户原来的版本一致，中文注释未动。
- **Verification**：`tests/test_step_tool_list.py` 5 项；连同缓存布局、执行器、循环、规划器测试共 127 passed；init.ps1 PASS，94 个 Python 文件，1644 passed、16 skipped。离线重建 `fix_inventory` 规划模式请求：步骤上下文 3,262 → 1,495 字符（少 54%），规划器提示 4,314 字符不变。
- **Live**：调参集规划模式各 1 次（用户“跑调参集”）：**11/11**，和改动前相同。调用次数相同的 8 题提示令牌都少了（每题少 957～2,481）；`billing_qa`、`sales_csv_qa` 各少一次调用；总数多出来的全在 `config_merge_qa`（上次碰巧没有中转站额外令牌，这次有，还多调了 3 次）。剔除它后提示令牌 1,052,578 → 991,284（少 5.8%）。报告在被忽略的 `output/bench-tune-plan-nodup.json`。状态改为 done。
- **提交**：2026-11-09 经用户授权提交；`tool_calling_step_runner.py` 只提交本轮两处改动，用户中文注释留在工作区；未推送。

### 2026-11-08 — tool-list-dedup-plan-001：执行步骤去掉重复工具清单的方案

- **Result**：用户指出规划模式以后仍会用到，这一项不该等默认改成循环再做。写了 [方案](docs/step_tool_list_plan.md)，打算交给 Codex 实现（tool-list-dedup-001）。核对结果：规划器调用模型时不带原生工具定义，必须保留清单；执行步骤每次都带原生定义，清单是重复的，只在序列化步骤上下文时去掉；团队负责人只在系统提示里看到可分配的工具名，看不到说明，这次不动。
- **Not done**：未实现；改完需要用户授权跑一次调参集规划模式评测；未提交。随后已实现，见下一条。

### 2026-11-08 — deps-cleanup-001、kb-index-cli-001：去掉没用的依赖，加建库命令

- **依赖**：只从 `pyproject.toml` 去掉 `langchain-mcp-adapters`。`langgraph` 那 4 个包虽然 `app/` 不再导入，但 `langchain` 1.2.10 要求 `langgraph`，删掉固定版本只会让版本浮动，所以保留并加注释（D-067）。没有卸载任何包。
- **建库命令**：`python -m app.knowledge 目录`，默认预演（只切块计数，不连任何服务），`--confirm` 才写入；跳过 `.git`、`.venv` 等目录；失败只报文件和异常类型；前 3 个都失败时停止（D-068）。`common.md` 加了用法。
- **Verification**：`tests/test_knowledge_cli.py` 10 项通过；对 `docs/` 实际预演：64 个文件、836 块、退出码 0；init.ps1 PASS，94 个 Python 文件，1639 passed、16 skipped。
- **Not done**：`--confirm` 没有对真实 Milvus 和嵌入接口跑过；目录里删掉的文件，其旧块不会自动删除。随后经用户授权提交，未推送。

### 2026-11-08 — cleanup-stage0-001：第 0 阶段清理遗留代码

- **目标与完成**：按 docs/stage0_cleanup_plan.md 删除九个旧文件；修改四篇指定文档的过时表述，纠正知识库建库代码的定位；保留在用源码、工具导出、压缩器和命名空间包。新增 D-066，更新功能状态、交接、测试数和文档索引。
- **启动检查**：原工作区 init.ps1 通过（101 个 Python 文件，1622 passed、16 skipped，50.56s）。旧清单的 AGENTS.md、DECISIONS.md、PROJECT_HANDOVER.md、progress.md、session-handoff.md 五项哈希漂移，清单外变化为无；旧清单原样保留，本轮另建清单。
- **验证**：新增 tests/test_stage0_cleanup.py 的七项 find_spec 守卫，删除前 4 项失败、3 项通过；git rm 权限失败后重测仍为 4 项失败、11 项通过；实际删除后，守卫与 tests/tools/test_mcp_integration.py、tests/tools/test_web_search_integration.py 合计 15 passed（2.74s），没有跳过。init.ps1 通过：92 个 Python 文件，1629 passed、16 skipped（50.69s）。示例使用本地测试子进程，HTTP 和模型响应是模拟，不是外部真实服务验收。
- **命令**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`；`.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp <系统 TEMP 下新建目录> tests/test_stage0_cleanup.py tests/tools/test_mcp_integration.py tests/tools/test_web_search_integration.py`；基线核对按 docs/harness/baseline.md。
- **执行差异**：git rm 因 .git/index.lock 写权限被拒绝（退出码 128），改为逐一删除同样九个文件并移除空 aiops 目录；没有改索引、暂存、还原、提交或推送。
- **证据与边界**：用户四个已修改文件的 SHA-256 与启动时一致，三个脚本仍为删除状态；依赖、锁文件、.env、模型默认值及 data/ 未修改。新清单为 docs/harness/history/baseline-2026-11-08-cleanup-stage0.json；deleted_tracked 包括九项本轮删除及三项用户删除。
- **独立复核**：按执行计划技能完成一次只读独立审查，未发现严重、重要或次要问题；审查确认删除范围、保留模块、守卫、文档和清单哈希一致。用户差异、历史记录及明确排除的后续项按范围约束处理；没有待修复或延后的小问题。
- **退出检查**：标准入口和全部确定性测试通过；状态与记录已同步；无未记录半成品；本轮 TEMP 目录已删除，仓库无 __pycache__ 或 .pytest_cache（不含虚拟环境）；Git 仅有用户原差异和本轮预期变化。本轮基线已核对：base ancestor OK、drift none、deleted-restored none、uncommitted-outside-manifest none。
- **审查与提交（Claude，同日）**：独立复跑守卫、两个保留示例和工具边界测试共 19 passed，init.ps1 92 个文件、1629 passed、16 skipped，基线四项正常。改正两处：文档索引里网页方案一行仍写“未实现”；`docs/agent_runtime.md` 英文段落里夹了半句中文，改回英文。随后按用户“修完再提交”提交，未推送。
- **风险与下一步**：未验证真实模型、外部服务或真实容器。由用户另行决定是否移除 langgraph 系列及 langchain-mcp-adapters 依赖、是否增加知识库建库命令；去掉规划模式重复工具清单仍需真实评测，本轮不做。

### 2026-11-08 — cleanup-stage0-plan-001：第 0 阶段清理方案

- **Result**：写了 [第 0 阶段清理方案](docs/stage0_cleanup_plan.md)，打算交给 Codex 实现（cleanup-stage0-001）：删除 `aiops/`（6 个文件）、只被它使用的 `app/agent/mcp_client.py` 和两个 `mcp_demo` 客户端；保留测试在用的 `time_server.py`、`web_search_client.py`，以及唯一的建库代码 `vector_index_service.py`（架构梳理把它算作遗留代码是错的）。去掉规划模式重复的工具清单、删除没人用的依赖、加建库命令都不在范围内。
- **Not done**：未实现；loop-web-001 和本方案已按用户“先提交”一起提交。

### 2026-11-08 — loop-web-001：网页可选单一循环

- **Goal**：按已确认方案实现启动默认与逐消息执行方式，恢复沿用原选择，默认仍 plan，不提交。
- **Completed**：服务 task_options/能力声明和校验、API 转发/拒绝、启动参数和中文页面选择框；原消息幂等与恢复循环历史保持；新增 D-065、common 启动命令和新基线。
- **Verification**：初始新测试 14 failed/1 passed；实现后完整针对性 111 passed（3.73s）；init.ps1 1622 passed/16 skipped（55.55s）；node --check 与独立审查通过。沙箱 TEMP 权限问题在原代码重现，有效环境原套件 1600/16 后再验本轮 1622/16。
- **Evidence**：[loop-web-2026-11-08.md](docs/harness/loop-web-2026-11-08.md)；feature_list loop-web-001；D-065。
- **Commits**：实现时无；2026-11-08 随后经用户授权（“先提交”）与第 0 阶段方案一起提交，用户原差异未提交、哈希不变。
- **审查后修复（同日，用户“修这两处”）**：`app.js` 遇到不返回 `runners` 的旧版服务时按只有“按计划”处理，发消息时不带 `runner`（旧版服务的选项校验不接受多余字段）；单一循环的进度文字由“正在规划步骤”改为“正在执行，不单独规划步骤”。`node --check` 通过；init.ps1 PASS，1622 passed、16 skipped；未在浏览器里验证。
- **Risks**：未做真实模型/浏览器交互，不改变默认值；TEMP 有效环境需要本轮使用的执行权限。
- **Next**：等待授权的新任务，真实网页验收和默认变更按方案 §7 另行处理。

### 2026-11-07 — loop-web-plan-001：网页可选单一循环的修改方案

- **Result**：写了 [修改方案](docs/loop_web_plan.md)，打算交给 Codex 实现：执行方式作为每个任务的选项，和模式、写文件、长期记忆并列，恢复时沿用；列出要改的文件、测试、记录和约束。默认改成循环不在这次范围内。
- **Not done**：未实现；未提交。

### 2026-11-07 — loop-resume-001：阻塞恢复后循环接着做

- **Result**：`todo_html` 页面检查跳过（文件没保留，用户不要重跑）。循环调用 `ask_user` 阻塞时把消息存在内存里，回答后接着做：回答作为那次调用的结果，之前读过的不再重读，预算算上提问前用掉的部分；对不上问题、目标改了或进程重启过时仍从头开始（D-064）。
- **Verification**：`tests/test_agent_loop.py` 19 项通过（新增 3 项，服务测试改为检查续上的消息）；init.ps1 PASS，1600 passed、16 skipped。
- **Not done**：没用真实模型验证；网页仍走规划；默认仍是规划；未提交。

### 2026-11-07 — loop-sandbox-check-001：单一循环沙盒场景真实验证

- **Result**：调参集开沙盒、循环模式各 1 次（用户“测沙盒模式”）：**10/11**；写文件的题都是读、写交付物、写测试、跑一次 `run_tests`、回答；调用 48 次、约 105 万令牌、333 秒；没有失败的工具调用，没有留下容器或临时目录。`todo_html` 错在纯文本匹配 `enter_key_and_trim`，文件没保留。报告在被忽略的 `output/bench-tune-loop-sandbox.json`。
- **Not done**：`todo_html` 的页面是否正常没查；没有规划模式的沙盒对比；未提交。

### 2026-11-07 — loop-write-check-001：单一循环写文件场景真实验证

- **Result**：调参集开写文件、循环模式各 1 次（用户“测写文件场景”）：**11/11**；7 道写文件的题都写了交付物，4 道回答题按答案通过；调用 41 次、约 86 万令牌、223 秒；没有失败的工具调用。每道写文件的题写完都读回一遍再回答，各多一次调用。报告在被忽略的 `output/bench-tune-loop-write.json`。
- **Not done**：沙盒模式没测；没有规划模式开写文件的对比；阻塞后恢复仍从头开始（已由 loop-resume-001 修复）；网页仍走规划；未提交。

### 2026-11-07 — loop-todo-001：单一循环少花调用在记账上

- **Result**：离线查明每次请求约 2 万令牌里只有 2～3 千是智能体自己的，约 1.8 万来自外部；修复类多出来的调用多半是 `todo`。循环提示改为只在长任务里用 `todo` 且不单独发；每次调用记下模型要的工具，评测报告带逐次令牌和 `todo` 次数（D-063）。
- **Verification**：`tests/test_agent_loop.py` 16 项通过；init.ps1 PASS，1597 passed、16 skipped。
- **Live**：调参集各 1 次（用户“跑调参集”）：**10/11**；调用 25 次（改前循环 44、规划 52），约 50 万令牌（约 88 万、约 109 万），168 秒（308、336）；`todo` 一次没用，每题 2～3 次调用。`fix_inventory` 被评分守卫判为 `forbidden_import`，报告不存答案，不知道导入了什么。报告在被忽略的 `output/bench-tune-loop-todo.json`。
- **Live（重跑）**：`fix_inventory` 单独重跑 1 次（用户“单独重跑这题”）通过，16/16，3 次调用、约 6.2 万令牌，答案里没有导入；上次失败属偶发。
- **Live（留出集）**：各 1 次（用户“跑吧”）：**12/13**（改前循环 12/13，规划 11/13）；调用 33 次（57、67），约 60 万令牌（约 132 万、约 128 万），218 秒（362、467）；`todo` 一次没用；`side_effects_qa` 仍错在 `mutates_arguments`。报告在被忽略的 `output/bench-holdout-loop-todo.json`。
- **Not done**：写文件和沙盒场景没测；阻塞后恢复仍从头开始（已由 loop-resume-001 修复）；网页仍走规划；默认仍是规划；未提交。

### 2026-11-07 — loop-trial-001：架构第 1 阶段试验（单一循环）

- **Result**：普通模式可以选用单一循环（`--single-runner loop`）：不调用规划模型，模型调用工具直到回复纯文本；`todo` 和 `ask_user` 由循环自己处理；默认仍是规划（D-062）。
- **Verification**：`tests/test_agent_loop.py` 13 项通过；init.ps1 PASS，1594 passed、16 skipped。
- **Live**：调参集各 1 次（用户“跑调参集”）：**11/11**，与规划模式相同；模型调用 44 次（规划 52）、约 88 万令牌（约 109 万，少 19%）、308 秒（336）。读后回答类每题 2 次调用、约 4.1 万令牌；修复代码类反而多用约 1 到 4 万令牌。报告在被忽略的 `output/bench-tune-loop.json`。
- **Live（留出集）**：各 1 次（用户“跑留出集”）：**12/13**（规划模式 11/13），`call_chain_qa` 变为通过，`side_effects_qa` 仍错在 `mutates_arguments`；调用 57 次（67）、用时 362 秒（467），令牌约 132 万（约 128 万，多 3%，中转站另加的令牌忽有忽无，噪声大）。报告在被忽略的 `output/bench-holdout-loop.json`。
- **Not done**：修复类多用令牌的原因没查；网页仍走规划；默认仍是规划；未提交。

### 2026-11-07 — arch-review-001：智能体架构梳理

- **Result**：写了 [架构梳理](docs/architecture_review.md)：根因是控制流以“计划步骤”为单位，任务状态里多项设计从未在生产路径上使用，限额分散在多层；建议以智能体循环为唯一控制流，规划、团队、提问改为循环里的工具，分五个阶段迁移，阶段 1 是可评测的试验。
- **Verification**：只读代码和搜索调用方核对；没有改代码，没有调用模型。
- **Not done**：第 8 节四个问题等用户回答；未提交。

### 2026-11-07 — hooks-live-001：hooks 第 4 步（真实验证）

- **Result**：调参集普通模式重跑 **11/11**，与加 hooks 前相同（52 次调用、336 秒、约 109 万令牌；之前 53 次、395 秒、约 111 万）。网页用真实模型走了人工确认：允许后才写入 `upper.txt` 且内容正确；拒绝后 `lower.txt` 没有写，模型改为询问用户是否授权。
- **Verification**：报告在被忽略的 `output/bench-tune-single-hooks.json`；网页检查用临时工作区、临时对话存储和生成的测试令牌，结束后关闭了服务和子进程。
- **Not done**：团队模式下的人工确认没有真实跑过；未提交。

### 2026-11-07 — hooks-approval-001：hooks 第 3 步

- **Result**：网页上写文件、运行代码前先问用户（允许、拒绝、本次运行都允许），运行原地等待，默认 300 秒没回答按拒绝；等待时间不计入步骤和团队时限（D-061）。
- **Verification**：`tests/test_web_approvals.py` 9 项通过；init.ps1 PASS，1581 passed、16 skipped；浏览器里用假模型检查过三种回答的显示和结果。
- **Not done**：没有用真实模型跑（设计稿第 4 步）；未提交。

### 2026-11-07 — hooks-builtin-001：hooks 第 2 步

- **Result**：新增审计记录（每次运行在会话目录 `audit/` 下写一份 JSON Lines，只有宿主记录）和对话令牌上限（`CONVERSATION_MAX_TOKENS`，超过后拒绝模型调用，运行以 `policy` 失败，重启后从归档结果累计）（D-060）。
- **Verification**：`tests/test_builtin_hooks.py` 8 项通过；init.ps1 PASS，1572 passed、16 skipped。
- **Not done**：没有真实运行；第 3 步（网页人工确认）未开始；未提交。

### 2026-11-07 — hooks-core-001：hooks 第 1 步

- **Result**：新增 `app/agent/hooks.py`（事件、观察型和拦截型 hook、`HookedTool`、`HookedModel`）；任务注册表包装每个工具对象，团队成员同样经过；服务包装底层模型并标明调用用途；运行时经 hooks 发事件，`on_event` 改为观察型 hook，收到的内容不变（D-059）。
- **Verification**：`tests/test_hooks.py` 13 项通过，原有测试不改全部通过；init.ps1 PASS，1564 passed、16 skipped。
- **Not done**：没有内置 hook（第 2、3 步）；没有真实运行；未提交。

### 2026-11-07 — hooks-design-001：hooks 设计稿

- **Result**：用户“先写 hooks 设计稿”。写了 `docs/hooks_design.md`：固定事件点，观察型和拦截型两种 hook，在工具对象和底层模型上包装以覆盖团队模式，四个内置 hook（进度、审计、对话费用上限、网页人工确认），第 11 节列了 5 个待确认问题和推荐答案。
- **Verification**：设计稿引用的现有接口都按实际位置核对过；没有改代码。
- **确认**：用户 2026-11-07 答“都按推荐”，记为 D-058。
- **Not done**：未实现；未提交。

### 2026-11-06 — bench-holdout-001：留出集首次运行

- **Result**：用户“跑留出集”。普通模式、每题 1 次、13 题，走中转站模型：**11/13 严格通过**（95% 区间 58%～96%）。没通过：`call_chain_qa`（6 项检查过 5 项，`unreachable` 错）、`side_effects_qa`（5 项过 4 项，`mutates_arguments` 错）；实现和修复类 6 题全部通过。共 67 次模型调用，467 秒，约 128 万令牌，没有工具调用失败。报告在被忽略的 `output/bench-holdout-single-d057.json`。
- **注意**：按 D-035，留出集的失败不能用来改提示词。有 3 题只用了 1.8 万～2.5 万令牌，其余都在 8.8 万以上，两道失败题都在这 3 题里，可能是中转站分到了别的上游，未核实。
- **Not done**：团队模式没有跑；未提交。

### 2026-11-06 — bench-tune-rerun-002：调参集重跑

- **Result**：用户“重跑调参集评测”。普通模式、每题 1 次、11 题，走中转站模型：**11/11 严格通过**（95% 区间 74%～100%；上次 6/11）。共 53 次模型调用（上次 137 次），395 秒，约 111 万令牌（上次约 88 万；中转站每次另加约 1.4 万～1.8 万输入令牌，总量不可直接比较）；没有工具调用失败。报告在被忽略的 `output/bench-tune-single-d057.json`。
- **注意**：上次用的是阿里云模型且在 D-037～D-057 之前，这次模型和智能体都变了，提升不能全部归于智能体改动。
- **Not done**：留出集（13 题）和团队模式没有跑；未提交。

### 2026-11-06 — 提交 D-052～D-057

- **Result**：用户“提交这批改动”。长文件读取、步骤工具额度、缓存记录、代码搜索、缓存诊断和请求开头稳定化（D-052～D-057）提交为 `fe0f9d9`，基线清单哈希另提交为 `bf0c8d5`；下面各条记录里的“未提交”均已过时。
- **Verification**：从暂存内容导出的目录里 1551 passed、16 skipped；基线核对四项正常。
- **Not done**：用户的 `README.md`、三个文件的中文注释、三个脚本的删除照旧不提交；没有推送。

### 2026-11-06 — prompt-cache-layout-001：让请求开头保持稳定

- **Result**：单独配置的模型服务带按对话生成的 `prompt_cache_key`（哈希，可关闭）；收尾请求保留工具列表并发送 `tool_choice: "none"`；本步上下文把各步相同的字段放前面，最后一步规则移到上下文之后（D-057）。
- **Verification**：`tests/test_prompt_cache_layout.py` 9 项通过，5 个旧测试按新顺序更新；init.ps1 PASS，1551 passed、16 skipped；基线清单已更新步骤执行器的哈希。
- **真实对比 1 次（已授权）**：成功；中转站接受两个新参数；一步内每次请求都 100% 保留上一次的开头；命中 32%（上次 28%），隔一次一中，剩下的未命中最可能在中转站；每次请求比上次多约 1.39 万令牌（规划请求也一样），是中转站另加的内容。
- **Not done**：未提交。

### 2026-11-06 — cache-diagnosis-001：逐次记录缓存命中和中转站缓存测试

- **Result**：每次模型调用记录输入、命中、输出和与上一次请求开头相同的比例，日志逐次输出；新增 `python -m app.agent.cache_probe` 测试模型服务的缓存是否稳定（D-056）。
- **Verification**：`tests/test_cache_call_log.py` 8 项通过；init.ps1 PASS，1542 passed、16 skipped。
- **真实测试 1 次（已授权，3 次调用）**：模型 `gpt-5.6-luna`；每次输入都是 22957 令牌，而固定文字只有约 4500，说明中转站每次另加约 1.8 万令牌；第 2、3 次各命中 22954，中转站对相同请求的缓存是稳定的。
- **真实任务 1 次（已授权）**：成功，6 次调用，约 10.9 万令牌，命中 28%。第 6 次（44840 令牌，工具次数用完后的收尾请求）不带工具列表，完全没命中；第 3 次没命中，原因未明。之后把“开头相同比例”改为按上一次请求保留多少计算。
- **Not done**：未提交；第二阶段未开始。

### 2026-11-06 — search-files-001：代码搜索工具

- **Result**：文件系统服务新增只读的 `search_files`，返回匹配的路径、行号和行内容，跳过敏感、隐藏、缓存目录和二进制、超大文件，结果有上限；`read_file` 说明里提示先搜再读（D-055）。
- **Verification**：`tests/test_search_files.py` 15 项通过，2 个工具名单测试已更新；init.ps1 PASS，1534 passed、16 skipped。
- **Live**：用户授权真实对比 1 次：成功，6 次调用、约 7.8 万令牌（上次 18.9 万，降 59%）；模型先搜索再按行读，节省主要来自工具调用合并成更少轮次，读入总量相近。
- **Not done**：未提交。

### 2026-11-06 — cache-usage-001：记录缓存命中

- **Result**：用量里新增命中缓存的输入令牌数和报告次数，运行日志、命令行用量行和网页回复底部都显示；服务商没报告时明确写“未报告”，不显示成 0（D-054）。
- **Verification**：`tests/test_cache_usage.py` 3 项通过，1 个命令行测试按新格式更新；init.ps1 PASS，1519 passed、16 skipped。
- **Live**：用户授权真实跑 1 次：任务成功（1 步、7 次调用、约 18.9 万令牌、71 秒），中转站报告输入 185859 中 72700 命中缓存（39%），7 次调用中 3 次带该字段。
- **Not done**：未提交。

### 2026-11-06 — step-wrap-up-001：工具次数用完时收尾、问答类一步完成

- **Result**：运行模式下一步的工具次数用完时，不再直接失败，而是拒绝超出的调用并请模型根据已读内容给出本步结论；规划提示要求解释、问答类任务优先一步完成（D-053）。
- **Verification**：`tests/test_step_tool_budget.py` 4 项通过；init.ps1 PASS，1516 passed、16 skipped。用户授权真实重跑 1 次：**成功**，1 步计划、6 次调用、约 17.7 万令牌、86 秒（修复前 24 次、51 万令牌、失败），回答抽查与代码一致。
- **Not done**：未提交；每轮重发本步工具结果的令牌开销未改。

### 2026-11-06 — long-file-reading-001：长文件读取修复

- **Result**：诊断网页上“解释 runtime.py”任务失败的原因并修复（D-052）：工具结果预算随对话模型窗口变化（新配置 `AGENT_MODEL_CONTEXT_TOKENS`），转存只存原文，读存档不再二次转存并提示从哪继续，`read_file` 支持按行读，规划提示要求读取和分析放在同一步。
- **Verification**：`tests/test_tool_result_budgets.py` 14 项通过，3 个旧测试按新行为更新；init.ps1 PASS，1512 passed、16 skipped。用户授权真实重跑 1 次：第 1 步按行读完 `runtime.py`，但第 2 步超过工具轮数，在 40 万令牌上限处停止（13 次调用、47.6 万令牌；修复前 24 次、51 万）。由此查出 MCP 适配器把每个结果拼了两遍，已修。
- **Live re-run 2**：去重后再授权跑 1 次：读取大小正确，第 1 步成功，但 3 步计划在第 2、3 步失败，停在令牌上限（18 次调用、62.8 万令牌）。剩余问题：计划拆得过细、步骤工具轮数用尽时已读内容作废、每轮重发本步工具结果。
- **Not done**：上述剩余问题未改；用户 `.env` 未改；未提交。

### 2026-11-06 — web-feedback-001：网页按试用反馈改进

- **Result**：回复里的 SVG 显示成图片（可查看代码、复制、下载），用户和 Aletheia 的话明显区分，输入框下方可按条选择执行模式、写文件和长期记忆（D-051）。后端由另开的会话完成，主会话核对并接上前端。
- **Verification**：`tests/test_run_options.py` 25 项、`tests/test_web_api.py` 24 项通过；假模型演示服务器上在内置浏览器里走过三种 SVG、深浅色、手机宽度和关写文件；init.ps1 PASS，1498 passed、16 skipped。演示服务器已停。
- **Not done**：没有用真实模型试过；未提交。

### 2026-11-06 — agent-model-endpoint-001：对话模型单独配置

- **Result**：新增 `AGENT_MODEL_BASE_URL`、`AGENT_MODEL_API_KEY`、`AGENT_MODEL`（D-050），只把智能体的对话模型切到另一个兼容接口，嵌入、知识库和长期记忆不变；地址和密钥必须同时填。`.env.example` 加了注释说明，用户的 `.env` 没动。
- **Verification**：`tests/test_agent_model_endpoint.py` 6 项通过；init.ps1 PASS，1472 passed、16 skipped。
- **Follow-up**：用单独地址时不再发送阿里云专用的 `enable_thinking`；按用户要求在 `.env` 末尾追加中转站配置（注释状态，等用户填密钥）和随机网页口令，原有内容未读未改。
- **Not done**：没有连过真实中转站；未提交。

### 2026-11-06 — web-ui-editorial-001：网页改为文档式排版

- **Result**：按用户上传的参考截图重做页面外观（D-049）：纸色背景、衬线标题、细分隔线、浅灰左导航、珊瑚色强调、黑色胶囊按钮、右侧“本会话”大纲；每个会话排成一篇文档。用户点名的 frontend-design 技能本会话加载不到，直接按截图实现。
- **Verification**：`tests/test_web_api.py` 23 项通过；假模型演示服务器上在内置浏览器里看了宽屏浅色、宽屏深色、手机宽度和记忆抽屉，修了 3 处（首轮大标题、三级标题偏小、大纲高亮位置）；init.ps1 PASS，1466 passed、16 skipped。演示服务器已停，临时文件已删。
- **Not done**：没有用真实模型试过；D-048 和本次改动都未提交。

### 2026-11-06 — web-ui-refresh-001：网页界面改版

- **Result**：按用户“过于简约单薄”的反馈重写页面三个文件（D-048）：品牌侧栏、会话分组和搜索、状态卡、欢迎页示例、头像和回复卡片、按计划对齐的步骤时间线、提问横幅、记忆抽屉、回复的安全格式显示；运行时 `plan` 事件增加 `step_ids`。
- **Verification**：相关测试 35 项通过（新增断言：步骤事件能按编号对上计划；页面只放行 SVG 命名空间这一个地址）；假模型演示服务器上在内置浏览器里看了宽屏浅色、宽屏深色、手机宽度，走过发送、时间线、格式化回复、提问回答、停止、新建会话、记忆抽屉，期间修了 6 处（示例文字居中、会话空提示位置、运行标签无颜色、新会话没及时出现在列表、斜体不显示、窄屏发送按钮跑到左边）；init.ps1 PASS，1466 passed、16 skipped。演示服务器已停，临时文件已删。
- **Not done**：没有用真实模型试过；未提交。

### 2026-11-06 — web-frontend-001：静态前端页面

- **Result**：新增 `app/api/static/`（`index.html`、`app.js`、`app.css`），后端提供页面并给所有响应加安全头（D-047）。页面能登录、列会话、看历史、发送并看逐步进度、停下后直接回答、取消、管理长期记忆，适配窄屏和深色。
- **Verification**：`tests/test_web_api.py` 增加 2 项（页面和文件的提供与安全头；脚本不写 HTML、不引用外部地址、令牌不进 `localStorage`）；用假模型演示服务器在内置浏览器里走完登录、进度、回答、取消、刷新、新建会话、记忆、窄屏宽屏、深色，发现并修了 4 处（停下的步骤显示成红色、历史里显示英文诊断、窄屏两个侧栏能同时打开、登录按钮不醒目）；init.ps1 PASS，1466 passed、16 skipped。演示服务器已停止，临时目录已删除。
- **Not done**：没有用真实模型走过网页流程（`web-live-001`，需授权次数）；未提交。

### 2026-11-06 — web-backend-001：后端接口

- **Result**：新增 `app/api/`（`runs.py` 运行管理和事件缓冲、`app.py` 接口、`__main__.py` 启动入口），配置增加 `WEB_*` 八项（`.env.example` 已写，`.env` 未动），服务增加 `resumable`（D-046）。
- **Verification**：新增 `tests/test_web_api.py` 21 项（后台运行和事件、断线续发、重复编号、阻塞后回答、会话占用和全局上限、取消、失败不带异常原文、输入校验、令牌、过期、关闭、记忆接口、启动检查）；实际启动冒烟检查通过（不调用模型）；init.ps1 PASS，1464 passed、16 skipped。
- **Not done**：前端（`web-frontend-001`）未开始；没有用真实模型走过网页流程；Windows 下 Ctrl+C 正常关闭未实测；未提交。

### 2026-11-06 — web-events-001：进度回调和会话历史

- **Result**：`AgentRuntime.run`、`RagAgentService.query_result` 增加可选的同步回调 `on_event`，依次收到 `route`、`plan`、每步的 `step` 记录（与 `step_log` 相同，不含模型原文）；回调出错不影响任务。服务增加 `list_sessions`、`session_turns`，归档增加 `exists`、`session_ids`（D-045）。
- **Verification**：新增 `tests/test_progress_and_history.py` 12 项；`tests/test_route_selection.py` 的假运行时 `run()` 接受关键字参数；init.ps1 PASS，1443 passed、16 skipped。
- **Not done**：后端接口（`web-backend-001`）未开始；未提交。

### 2026-11-06 — web-design-001：网页后端和前端设计稿

- **Result**：写了 [设计稿](docs/web_backend_design.md)：运行和 HTTP 请求分开、服务器推送进度事件、阻塞恢复和取消接口、错误码不带异常原文、本机令牌身份、全局运行上限、静态页面，以及实施顺序（`web-events-001` → `web-backend-001` → `web-frontend-001` → `web-live-001`）。决策 D-044（提议）。
- **Verification**：只改文档；稿中引用的方法和文件都按源码核对；init.ps1 PASS，1431 passed、16 skipped。
- **Not done**：未实现；未提交。用户对第 11 节四个问题答“都按推荐”，已写回设计稿和 D-044。

### 2026-11-06 — blocked-resume-prompt-001：阻塞直接提问、恢复直接用回答

- **Result**：查明阻塞理由原样变成显示给用户的问题，而提示只要求“写需要回答的问题”且另一句要求“说明阻塞”；恢复后用户回答只在 `resolved_questions` 里，提示从未说明。步骤提示改为：阻塞时写一句直接问用户的问题；`resolved_questions` 是权威回答，直接使用，不去文件、归档、对话里找（D-043）。
- **Verification**：新增 `tests/agent/test_blocked_and_resume_prompts.py` 3 项；团队压缩档位测试窗口 7000 改 7500；init.ps1 PASS，1431 passed、16 skipped。
- **Live**：2026-11-06 经用户授权用真实模型重测 2 轮：阻塞时显示“请告诉我你需要翻译成英文的那句中文是什么？”；恢复后的步骤没有调用任何工具，2 次模型调用、5532 令牌（修复前 6 次、21980 令牌，含 3 次读取归档和 1 次列目录）；日志无警告和错误。
- **Not done**：**未提交**。

### 2026-11-06 — e2e-live-001：真实环境端到端验证

- **授权**：用户选择“临时容器”“启动，验完停掉”“3 轮任务”（模型调用合计上限 40 次）。
- **环境**：下载 `postgres:16-alpine`，临时容器只监听 `127.0.0.1:55432`，密码随机生成且只放在会话临时目录；用 `docker compose start` 启动 `deploy/milvus`，只用单独集合 `agent_memories_e2e`；对话归档改到会话临时目录；智能体根目录是空的临时目录。
- **长期记忆层（真实 PostgreSQL + Milvus + DashScope 嵌入，5 次嵌入调用）**：10 项全部通过：装配、两条显式写入、列出、召回排第一、另一用户召回为空、删除、重复删除返回否、删除后不再召回、PostgreSQL 只剩保留的一条、Milvus 向量与数据库一致且集合里没有其他用户。
- **智能体交互流程（真实模型，3 轮，共 10 次模型调用）**：第 1 轮召回长期记忆并正确回答格式偏好（2 次调用、4456 令牌）；第 2 轮任务阻塞（2 次、5007 令牌）；第 3 轮输入回答后同一任务恢复完成翻译（6 次、21980 令牌）；`/memories` 列出记忆；日志无警告和错误；归档按用户写入 `user-scoped`。
- **发现的问题**：（1）阻塞时显示的是停下的理由（“用户尚未提供待翻译的中文原句……”），不是要问用户的问题；（2）恢复后的步骤为一句翻译调用了 3 次 `read_context_artifact`（2 次失败）和 1 次 `list_directory`，令牌是前两轮的 4～5 倍。
- **清理**：删除 `agent_memories_e2e` 集合（`biz` 保留）、删除临时数据库容器（镜像保留）、Milvus 三个容器回到停止状态、删除临时文件和密码文件；仓库内无缓存，`data/` 无新增。
- **Not done**：模型调用重试没有在真实故障下触发；调参集整体重跑、留出集、团队模式未跑。

### 2026-11-06 — longterm-memory-001：长期记忆接入

- **Result**：在用户未提交的召回接入上继续：新增 `LONGTERM_MEMORY_DSN` 等配置（默认关闭）和装配函数 `open_longterm_memory()`；召回预算改为可配置（默认 600），条目字段精简，加“记忆只作参考”的策略说明，修正去重缺陷；新增 `remember_explicit`（不调用模型）和 `list_memories`；命令行新增 `--user-id`，交互模式新增 `/remember`、`/memories`、`/forget`（D-042）。
- **Verification**：新增 `tests/test_longterm_memory_wiring.py` 19 项（召回、降级、缺用户、超限删除、显式写入、装配与关闭、交互命令、入口）；一处旧测试的替身改为接受关键字参数；init.ps1 PASS，1428 passed、16 skipped。
- **Not done**：没有连接真实 PostgreSQL 和 Milvus；按用户删除全部记忆、数据库与向量库对账、召回质量（关键词通道、相关性阈值）未做。已按用户“只带长期记忆部分”提交：`context_manager.py`、`rag_agent_service.py` 里用户的长期记忆改动随本次一起提交；README、三个文件的中文注释和三个脚本的删除仍未提交，基线清单已刷新。

### 2026-11-06 — interactive-cli-001、archive-default-001：产品形态（交互与归档）

- **Result**：命令行不给目标或加 `-i` 进入交互模式：一个会话连续多轮，被阻塞的任务由下一行原地回答，`/new` 放弃，`/help`、`/exit`，每轮单独预算（D-040）。对话归档默认开启，存到项目根下的 `data/conversations`，设为空关闭；评测和测试不写这个目录（D-041，由并行会话实现，主会话检查后合入）。
- **Verification**：新增 `tests/agent/test_interactive_cli.py`、`tests/test_archive_default.py`、`tests/conftest.py`；真实入口冒烟（只用命令，不调用模型）；init.ps1 PASS，1409 passed、16 skipped；`data/conversations` 运行前后都不存在。
- **Not done**：未用真实模型跑交互流程；长期记忆需用户决定如何处理其未提交的改动；已按用户“先提交这两项”提交。

### 2026-11-06 — 清洁状态检查与基线刷新

- **Result**：按 `docs/harness/clean-state-checklist.md` 检查：仓库内无 `__pycache__`、`.pytest_cache`、`.pyc`；`tmp/` 为空；无 aletheia 容器；功能表无 `in_progress` 条目；删除了本轮直接运行 pytest 留下的系统临时目录 `pytest-of-*`。2026-11-04 基线清单已过时（中文注释和后续提交改变了哈希，清单中被删的测试已按 D-030 恢复），新建 `docs/harness/history/baseline-2026-11-06.json` 并把核对命令、文档索引、清单和交接指向它；核对结果：基础提交 OK、drift none、deleted-restored none。
- **Not done**：系统临时目录里有 2026-11-04 留下的 `Aletheia-cleanup-*`、`aletheia-cleanup-check-*` 和 `aletheia-cleanup-current.txt`，不是本会话产生，未删除；`output/` 下的评测报告在被忽略的目录中保留；已按用户“先提交基线刷新”提交。

### 2026-11-06 — model-retry-001：模型调用偶发错误重试

- **Result**：查明服务商出错时整条链路都不重试（SDK `max_retries=0`、运行时直接失败）。模型适配层改为对超时、连接失败、限流、5xx、无效或空回复最多再请求 2 次（0.5 秒、1 秒退避）；截断、过滤、拒答、上下文超长、其他 4xx、工具参数解不开不重试；预算用完不重试（D-039）。
- **Verification**：新增 `tests/agent/test_model_retry.py`；两处观察单次失败的旧用例改为 `max_retries=0`；init.ps1 PASS，1397 passed、16 skipped。
- **Not done**：未用真实模型验证；已按用户“提交”提交。

### 2026-11-06 — flaky-summary-e2e-001：修复摘要生命周期测试的偶发失败

- **Result**：`timeout` 用例给摘要调度器的 0.15 秒也要覆盖等对话锁和读档案目录，机器负载高时两次尝试都在调用服务商之前就超时，测试等不到服务商被调用而失败。把读档案目录放慢 0.2 秒即可稳定复现。该用例的超时改为 1 秒；服务商在这个用例里永不返回，超时照样触发。生产默认 30 秒不变。
- **Verification**：放慢 0.2 秒和 0.5 秒的副本都通过；该文件连跑 3 遍各 10 项通过；init.ps1 PASS，1374 passed、16 skipped，另跑一遍全量也全部通过。
- **Not done**：已按用户“提交”提交。

### 2026-11-06 — grader-guard-001：评分守卫放行无害名字

- **Result**：经用户授权跑 1 次 `semver_compare` 并保存答案：答案只有代码，唯一被拒的是 `__slots__`。评分器守卫的允许名单加入无害的数据模型名字，危险名字仍拒绝；放宽后这份答案 39/39 通过（D-038）。保存的答案已删除。
- **Verification**：`tests/test_bench_graders.py` 新增放行和拒绝用例；init.ps1 PASS，1374 passed、16 skipped。
- **Not done**：`config_merge_qa` 的无效响应、`sales_csv_qa` 的手算错误未处理；已按用户“提交”提交。

### 2026-11-06 — final-answer-form-001：最后一步按目标形式交付

- **Result**：步骤上下文新增 `final_step`；计划最后一步的系统提示追加规则：结果就是最终答案，要完整，目标规定了形式时只输出那个内容，不加说明或围栏（D-037）。宿主不改写答案。
- **Verification**：新增 `tests/agent/test_final_step.py` 3 项；init.ps1 PASS，1367 passed、16 skipped。
- **Live**：2026-11-06 经用户授权重测 2 次（普通模式各 1 次）：`fix_rate_limiter` 严格通过 24/24，答案只有代码；`semver_compare` 被评分器的静态守卫以 `forbidden_name` 拒绝（代码用了禁止的名字，报告不存原文，无法确定是哪个；下次可加 `--keep-answers` 查看）。报告在被忽略的 `output/bench-final-step.json`。
- **Not done**：`semver_compare` 被守卫拒绝的原因未查；已按用户“提交”提交。

### 2026-11-06 — output-cap-001：调高单次输出上限

- **Result**：`CONTEXT_OUTPUT_TOKENS` 默认值从 2048 改为 8192（`app/config.py`、`.env.example`、`docs/agent_runtime.md`），输入空间剩 23552（D-036）。
- **Verification**：新增 `tests/test_config_defaults.py`；init.ps1 PASS，1364 passed、16 skipped。
- **Live**：2026-11-06 经用户授权重测 2 次（普通模式各 1 次）：两题都不再超出输出上限；`todo_html` 严格通过 11/11；`semver_compare` 代码宽松判定全对，但答案在代码外夹了说明文字，严格判定不通过（与 `fix_rate_limiter` 同类）。报告在被忽略的 `output/bench-output-cap.json`。
- **Not done**：代码类答案夹说明文字的问题未修；已按用户“提交”提交。

### 2026-11-06 — eval-expansion-001：扩充评测

- **Result**：评测任务从 4 个扩到 24 个（只读问答 11、按规格实现 6、对照规格修复 6、前端 1），分为调参集 11 个、留出集 13 个；`--tasks all` 默认只跑调参集，留出集要显式 `--split holdout`；报告每格附 Wilson 95% 区间，新增 `by_split` 汇总（D-035）。问答题和代码题由两个并行会话编写，主会话抽查后合入。
- **Verification**：新增 `tests/test_bench_tasks.py`（任务格式、参考解通过、修复类原文件失败、区间和划分选择）；init.ps1 PASS，1363 passed、16 skipped。
- **Live**：2026-11-06 经用户授权 11 次，调参集普通模式各跑 1 次：6/11 通过（54.5%，95% 区间 28%～79%）。失败 5 个：`semver_compare`、`todo_html` 撞到单次输出上限 `CONTEXT_OUTPUT_TOKENS=2048`（ModelOutputLimitError）；`config_merge_qa` 服务商返回无效响应（ModelResponseError）；`fix_rate_limiter` 代码本身全对（宽松 24/24），但答案夹了说明文字，严格判定不通过；`sales_csv_qa` 7 个键算错 2 个（手算金额）。报告在被忽略的 `output/bench-tune-single.json`。
- **Not done**：输出上限和答案夹说明文字两个问题未修（改 `CONTEXT_OUTPUT_TOKENS` 默认值需用户决定）；留出集和团队模式未跑；已按用户“提交”提交。

### 2026-11-06 — observability-001：可观测性

- **Result**：随上下文传递的计量器统计模型调用和令牌，并承载令牌和总时长预算（`--max-tokens`、`--max-seconds`，用完失败类型为 `budget`）；每次步骤尝试记录结果、执行方式和工具调用（只含工具名、成败、耗时、异常类型）；命令行 `--result-json` 输出结构化结果，评测优先读它；标准库日志转进 `loguru`，统一按 `ALETHEIA_LOG_LEVEL`（默认 INFO）输出（D-034）。
- **Verification**：新增 `tests/agent/test_observability.py` 19 项；init.ps1 PASS，1306 passed、16 skipped。
- **Not done**：真实模型下的令牌数和评测报告未验证（需授权次数）；长期记忆和压缩器的模型调用不计入；路由和团队记录仍从标准错误里解析。已按用户“提交”提交。

### 2026-11-06 — reliability-001：执行可靠性

- **Result**：步骤结果和计划的形状错误进入有上限的修复，规划超时重试一次（D-031，规划器部分由并行会话完成）；写入或运行类工具出错、或用过它们后超时，任务直接失败不再重新规划，普通模式的未授权工具在本轮反馈（D-032）；失败分类和中文说明、次数用完时保留根因、异常只记类型名、澄清不占重新规划次数、开沙盒时单步时限 120 秒（D-033）。
- **Verification**：新增可靠性测试 24 项和规划器修复测试；init.ps1 PASS，1287 passed、16 skipped。
- **Known issue**：`tests/agent/test_summary_lifecycle_e2e.py` 用 0.03～0.15 秒的时限，在提交版本上也约 5 次失败 2 次，与本次改动无关；2026-11-06 已查明并修复（flaky-summary-e2e-001）。
- **Not done**：失败步骤保留已观察到的工具结果（core P1-2）、整件任务的总时限和令牌预算（core P1-5）、runner 声明式契约（core P1-7）；真实模型复测未做。已按用户“提交”提交。

### 2026-11-06 — tests-restore-001 和 security-boundary-001：补测试和安全边界

- **Result**：恢复 48 个历史测试文件（D-030，取代 D-011）；文件工具拒绝读写密钥和版本库内部文件、拒绝写入自动执行位置；沙盒遮蔽密钥并只读挂载已有的自动执行位置；沙盒工具的 MCP 超时放宽到 90 秒（D-029）。
- **Verification**：init.ps1 PASS，1218 passed、16 skipped；开启真实容器测试后全量 1231 passed、3 skipped；新增受保护路径测试 28 项和真实容器测试 1 项；无残留容器，源码目录无缓存。
- **Known limits**：读写运行仍能新建不存在的自动执行目录；普通模式单步时限 60 秒短于沙盒调用的 90 秒。
- **Evidence**：[DECISIONS D-029、D-030](docs/DECISIONS.md)；审阅报告在被忽略的 `output/maturity-review/`。已按用户“提交”提交。

### 2026-11-05 — tidy-001：项目整洁性检查

- **Result**：删除 `tmp/pytest-harness-*` 10 个旧测试目录（路径过长导致之前删不掉）和本会话的 TEMP `pytest-of-*`；修正 AGENTS、项目总交接和本文件里“tests 只剩冒烟测试”的过时说明。
- **Kept**：外部备份、`aletheia-cleanup-check-*`、`output/pdf`、Milvus 容器、`app/agent/mcp_demo`（README 引用的示例）。
- **Verification**：仓库无未跟踪文件；`tmp/` 为空；无沙盒残留容器；init.ps1 PASS。已按用户“提交”提交。

### 2026-11-05 — readonly-run-001：只读任务用只读挂载运行代码

- **Result**：沙盒新增 `run_python_code`，工作区只读挂载，只交给只读任务。决策 D-028。
- **Verification**：当前 tests 167 passed；真实容器测试 12 passed；init.ps1 PASS；历史回归与上次相同。
- **Live**：授权 6 次 billing_qa，严格通过 6/6（D-027 后 3/6），工作区全部未改动。
- **Evidence**：[route-auto-2026-11-05.md](docs/harness/route-auto-2026-11-05.md)。已按用户“提交”提交。

### 2026-11-05 — answer-readonly-001：最终答案只取最后一步，只读任务收回写入工具

- **Result**：普通模式最终答案改为最后一步的结果；auto 路由同时判断是否需要写文件，不需要时宿主收回 `write_file`、`run_tests`、`run_python`。决策 D-027。
- **Verification**：路由测试 28 passed；当前 tests 160 passed；init.ps1 PASS；历史回归新增 1 项预期失败（旧测试断言拼接答案），其余与基线相同。
- **Live**：授权 6 次，严格通过 3/6（改前 0/6），工作区全部未改动；2 次 `fee_200_15` 算错（没有运行工具），1 次 `StepFormatError`（模型直接输出答案 JSON）。
- **Evidence**：[route-auto-2026-11-05.md](docs/harness/route-auto-2026-11-05.md)。**未提交**。

### 2026-11-05 — bench-qa-001 补充：只取最后一步的对照

- **Result**：评测器新增“只取最后一步”的对照评分（不改运行时）。真实 6 次 billing_qa：拼接答案 0/6，只取最后一步 5/6；但 4/6 次在只读任务里改动了工作区，完整严格通过只有 1/6。
- **Verification**：问答测试 24 passed；当前 tests 152 passed；历史回归基线不变；init.ps1 PASS。
- **Evidence**：[route-auto-2026-11-05.md](docs/harness/route-auto-2026-11-05.md)。**未提交**。

### 2026-11-05 — bench-qa-001：只读问答任务与路由真实评测

- **Result**：新增只读问答任务 `billing_qa` 和 `answer_json` 评分器，评测器统计路由是否正确。真实 6 次（auto、沙盒）：**路由 6/6 正确**；parse_duration 3/3 严格通过；billing_qa 0/3，原因是普通模式把所有步骤结果拼成最终答案，JSON 前面混有说明文字，另有 1 次在只读任务里写了文件。
- **Verification**：问答测试 18 passed；当前 tests 146 passed；历史回归基线不变；init.ps1 PASS。
- **Evidence**：[route-auto-2026-11-05.md](docs/harness/route-auto-2026-11-05.md)。**未提交**。待决定：普通模式最终答案是否改为只取最后一步。

### 2026-11-05 — route-auto-001：按任务动态选择执行模式

- **Result**：新增 `--execution-mode auto`，每个任务先按宿主规则、再用一次模型判断选择普通或团队，任何异常退回普通模式。决策 D-026。顺带删除评测器遗留的 `--team-version`。
- **Verification**：路由测试 20 passed；当前 tests 128 passed；历史回归基线不变；init.ps1 PASS。
- **Evidence**：[route-auto-2026-11-05.md](docs/harness/route-auto-2026-11-05.md)。真实运行未验证；**未提交**。

### 2026-11-05 — team-v1-removal-001：删除团队模式 V1

- **Result**：V1 团队和早期原型代码已删除；团队模式只剩 V2，`--team-version` 移除。决策 D-025。
- **Verification**：当前 tests 108 passed；init.ps1 PASS；历史回归 1022 passed，只多出 4 个 V1/原型测试文件无法收集。
- **Evidence**：[team-v2-2026-11-04.md](docs/harness/team-v2-2026-11-04.md)。**未提交**。

### 2026-11-05 — team-v2-evidence-001 / team-v2-default-001：保住格式出错时的交付，默认切到 V2

- **Result**：宿主自己记录执行者的工具结果，最终报告格式出错不再丢掉已测通的交付（D-023）；命令行和评测器默认 V2，V1 保留（D-024）。
- **Verification**：V2 测试 44 passed；当前 tests 139 passed；完整历史 1090 passed；init.ps1 PASS。
- **Evidence**：[team-v2-2026-11-04.md](docs/harness/team-v2-2026-11-04.md)。**未提交**。
- **Live（授权 3 次）**：严格 2/3；一次交付违反“只用 re”被评分器拒绝，评测器已补记 `guard_reason`。本批未出现格式错误，D-023 主路径未在真实运行中触发。

### 2026-11-04 — team-v2-acceptance-003：独立验收改为默认关闭

- **Result**：沙盒 V2 默认回到执行者自测门槛；`--team-acceptance` 可选开启。决策 D-022。
- **Verification**：当前 tests 135 passed；完整历史 1090 passed；init.ps1 PASS。
- **Evidence**：[team-v2-2026-11-04.md](docs/harness/team-v2-2026-11-04.md)。**未提交**。

### 2026-11-04 — team-v2-acceptance-002：加固验收流程

- **Result**：验收者单独计数并自动重试；失败后从工作目录找回验收文件；没有验收测试时退回自测门槛；未授予的工具会被点名。决策 D-021。
- **Verification**：V2 测试 40 passed；当前 tests 135 passed；完整历史 1090 passed；init.ps1 PASS。
- **Evidence**：[team-v2-2026-11-04.md](docs/harness/team-v2-2026-11-04.md)。**未提交**。
- **Live（授权 3 次）**：严格 1/3；一次 21/21 的交付因验收测试写错被判 blocked。汇总：自测门槛 6/7，独立验收 3/6 且成本 2–4 倍。结论：当前模型上独立验收弊大于利。

### 2026-11-04 — team-v2-acceptance-001：独立验收测试决定 V2 是否完成

- **Result**：执行者自测通过后，宿主委派独立验收者按目标写 `test_acceptance*.py`，并亲自运行；通过才算完成。执行者不能改验收文件；负责人可用 `verify` 重写验收测试。决策 D-020。
- **Verification**：新增 7 项测试；当前 tests 133 passed；完整历史 1090 passed；init.ps1 PASS。
- **Evidence**：[team-v2-2026-11-04.md](docs/harness/team-v2-2026-11-04.md)。**未提交**。
- **Live（授权 3 次）**：严格 2/3，完成的两次都是 21/21，没有再接受错误答案；但第 1 次文件 21/21 却因验收者失败、委派用完而 blocked；耗时与调用约为之前的 2–4 倍。

### 2026-11-04 — team-v2-context-001：V2 执行者不再因写文件超窗

- **Result**：视图压缩移到 `app/agent/model_view.py`；V2 执行者两档压缩（60% 去掉被取代的文件内容，85% 只保留最近一次写入的内容）。
- **Verification**：新增 3 项测试（含超窗复现）；当前 tests 125 passed；完整历史 1090 passed；init.ps1 PASS。
- **Evidence**：[team-v2-2026-11-04.md](docs/harness/team-v2-2026-11-04.md)。**未提交**。
- **Live（授权 3 次）**：严格 2/3；超窗从 3/4 降到 1/3，剩下的一次已补（第二档也压缩 run_python 代码与旧输出）。新发现：执行者请求未授予的工具即失败；宿主只凭执行者自测就接受了 14/21 的答案。

### 2026-11-04 — team-v2-001：团队模式 V2（编排者—执行者）

- **Result**：先把 V1 恢复改动提交为基线 `480f6fb`；新增 `app/agent/team_v2.py`，负责人通过 `delegate` 委派给全新上下文的普通模式执行器，宿主判定完成；`--team-version 2` 选择（默认仍为 1）。决策 D-019。
- **Verification**：新增 19 项测试；当前 tests 114 passed；完整历史 1090 passed（3 项已知失败不变）；init.ps1 PASS。
- **Cleanup**：TEMP 与 `__pycache__` 已删除，无沙盒容器残留。
- **Evidence**：[team-v2-2026-11-04.md](docs/harness/team-v2-2026-11-04.md)。V2 改动**未提交**。
- **Live run（授权 2 次）**：0/2，都在约 6 秒的规划阶段失败（单步规划的输出没通过校验，也没有修复），没进入团队。已改为不调用模型的固定单步计划，并补了运行时端到端测试；当前 tests 116 passed，历史 1090 passed。
- **Live run 2（授权 2 次）**：0/2，进入了团队，但负责人第一次请求就被服务方拒绝（BadRequestError）：JSON 模式要求消息里出现 “json”，且只能输出 JSON。负责人的最终回答已改为普通模式的 JSON 结果格式；当前 tests 122 passed，历史 1090 passed。
- **Live run 3（授权 2 次）**：**2/2 严格通过**。parse_duration 132 秒（第一个执行者上下文超窗失败，负责人重新委派后通过，21/21）；fix_inventory 39 秒（一次委派，16/16）。每项仅 1 次，样本小。
- **稳定性对照（授权 12 次）**：V2 6/6（parse_duration 3/3、fix_inventory 3/3），普通模式 4/6（parse_duration 1/3）。V2 弱点：parse_duration 第一个执行者常超出上下文窗口，重新委派使耗时翻倍。已提交 `29c2efe`，本轮只改记录。

### 2026-11-04 — team-recovery-001：团队恢复能力（诊断建议第 1、2 项）

- **Result**：成员只因协议错误失败时，交回负责人处理（团队最多 2 次）；修复次数改为每次运行重置（团队合计最多 8 次）。决策 D-018。
- **Verification**：新增与更新测试 11 passed；历史 Team + 路由 84 passed；完整历史 1090 passed（3 项已知失败不变）；当前 tests 82 passed。
- **Cleanup**：TEMP 已删除；`__pycache__` 已删除。
- **Evidence**：[team-recovery-2026-11-04.md](docs/harness/team-recovery-2026-11-04.md)
- **Live rerun（授权 2 次）**：0/2；两个执行成员都写完、跑过测试并交出候选，之后都在 `create_reviewer` 的宿主检查上终止。修复了评测器失败信息被文件内容覆盖的缺陷，轨迹记录宿主固定报错文字。
- **审查成员工具检查**：已改为可恢复的拒绝。再次真实重跑 2 次：两次都在负责人第一次调用就因未注册的工具名（KeyError）失败，已确定性复现。
- **未注册工具名**：已改为有上限的修复。再次重跑 2 次：parse_duration 第一次走到审查通过（三种恢复都生效），随后团队工具预算 32/32 用完；fix_inventory 评测器超时。
- **沙盒团队预算**：已调高。再次重跑 2 次：两次都因会话上下文超出 32768 窗口（ContextBudgetError）失败；负责人在审查通过后从不结束任务。
- **视图压缩与 finish_ready**：已实现。再次重跑 2 次：都在 700 秒被评测器终止，没有轨迹（成功轨迹是 INFO 级别；团队时限按步骤计算）。已补诊断：评测器强制输出轨迹、失败时也给文件评分。
- **Next**：经授权用更长的评测器时限再跑

### 2026-11-04 — team-diagnosis-001：团队模式失败诊断（用户授权 2 次）

- **Result**：团队模式 0/2。轨迹显示两次都是工作成员在第一次运行内连续 3 次产生无法解码的工具参数，2 次修复用完后抛出；成员失败直接终结整个团队。负责人修复次数分别为 0/2、2/2。
- **Fixes**：轨迹现在能展开嵌套的异常组，并记录被拒调用的工具名和参数长度（不含内容）。
- **Cleanup**：报告、日志已删除；没有残留容器；没有 `__pycache__`。
- **Evidence**：[team-diagnosis-2026-11-04.md](docs/harness/team-diagnosis-2026-11-04.md)
- **Next**：等用户决定修复方案（成员失败交回负责人、修复次数按运行重置、候选结果不内嵌代码）

### 2026-11-04 — sandbox-step4-001：沙盒真实评测（用户授权 4 次）

- **Result**：普通模式严格 **2/2**（parse_duration 21/21、fix_inventory 16/16，每个任务都在沙盒中运行了约 7 次）；团队模式 0/2，两次都在任何沙盒运行之前就失败了（一次是修复次数用完后仍有参数 JSON 错误，一次失败类型被评测运行器的过滤规则拒绝，记成 unrecognized）。
- **Fixes**：评测运行器的失败过滤规则允许数字、下划线、句点和逗号，并补了测试。
- **Cleanup**：答案、报告、日志已删除；没有残留容器；没有 `__pycache__`。
- **Evidence**：[sandbox-live-2026-11-04.md](docs/harness/sandbox-live-2026-11-04.md)
- **Next**：团队参数错误和失败诊断（待办 team-failure-diagnosis-001，优先级已提为 1）

### 2026-11-04 — sandbox-step3-001：沙盒第三步，接入运行工具

- **Goal**：把 `run_tests` / `run_python` 接入两种模式；团队必须先有通过的运行，才能结束任务。
- **Completed**：沙盒 MCP 服务；命令行 `--sandbox`、`--team-timeout`（在调用模型前检查镜像）；团队结束门槛 `require_passing_run`；评测集 `--sandbox`；决策 D-017。
- **Verification run**：本步测试 14 passed；真实容器测试 11 passed（含 MCP 端到端）；全量回归 1160 passed、14 skipped；没有调用真实模型。
- **Evidence recorded**：feature_list.json → sandbox-step3-001；设计方案第 6 节实施状态；bench/README.md
- **Commits**：无
- **Known risks**：普通模式每步只有 4 轮、8 次工具调用，写→跑→改的循环可能不够；团队默认 180 秒，跑代码可能不够（可用 `--team-timeout` 加长）
- **Next best action**：第四步，用 `--sandbox` 跑真实评测（需用户授权和次数上限）

### 2026-11-04 — sandbox-step2-001：沙盒第二步，镜像与执行器

- **Goal**：构建沙盒镜像，实现宿主侧执行器，并用破坏性测试证明限制有效（用户确认 Docker 已运行，并授权拉取基础镜像）。
- **Completed**：`deploy/sandbox/Dockerfile`；`app/tools/sandbox/executor.py`（固定限制、请求预校验、读取时截断、超时删除容器、只清理自己标签的容器）；决策 D-016。
- **Verification run**：执行器单元测试 12 passed、1 skipped；真实容器测试 10 passed（需显式开启）；全量回归 1146 passed、13 skipped；init.ps1 PASS。
- **Evidence recorded**：feature_list.json → sandbox-step2-001；设计方案第 8 节实施状态
- **Commits**：无
- **Known risks**：执行器还没有交给模型使用；Docker 不防内核漏洞（设计第 10 节）；Windows 账户无法创建链接，所以链接测试被跳过
- **Next best action**：第三步，把 `run_tests` / `run_python` 接入两种模式，只给执行成员

### 2026-11-04 — sandbox-step1-001：沙盒第一步，受限写入

- **Goal**：按沙盒设计第 5 节开放受限写入（用户采纳 Docker 方案后说“开始”）。
- **Completed**：只有配置写根时团队模式才获得 `write_file`；审查成员不得获得写入或运行工具，并在负责人提示中说明；评测集新增 `--write` 文件交付模式；决策 D-015。
- **Verification run**：新增测试 7 个，当前测试共 41 个全部通过；在提交版本上叠加本轮文件后跑全量测试，1134 passed、2 skipped。对照工作区跑历史测试有 3 个失败，原因是用户删除了 `scripts/`，以及仅复制测试目录导致的路径问题，与本轮改动无关。没有调用真实模型。
- **Evidence recorded**：feature_list.json → sandbox-step1-001；[设计方案](docs/sandbox_execution_design.md) 第 5 节状态
- **Commits**：无
- **Known risks**：Docker 引擎当天无法连接（进程未运行），第二步无法开始
- **Next best action**：用户确认 Docker Desktop 正常运行后，开始第二步（构建镜像、沙盒执行器、破坏性测试）

### 2026-11-04 — capability-phase1-001：第一轮真实评测（用户授权 6 次）

- **Result**：严格 0/6。single 3/3 completed，但答案都夹带说明文字；宽松诊断：fix_inventory 16/16、parse_duration 20/21、todo_html 11/11，浏览器 9/9。team 3/3 failed（失败类型因运行器正则 bug 丢失，已修复）。
- **Fixes**：运行器的失败过滤；HTML 评分器的常量键误判；新增宽松诊断字段（严格口径不变）；grader 测试 16 个。
- **Cleanup**：保存的答案、报告、本地 http 服务和浏览器标签都已删除或关闭；localStorage 已清除。
- **Next**：backlog deliverable-format-001（交付格式），team-failure-diagnosis-001（team 重跑 3 次，需授权）。

### 2026-11-04 — capability-phase1-001：可恢复的参数错误 + 评测集

- **Goal**：增强路线阶段 1，即错误恢复、错误类型可见、固定评测集。
- **Completed**：`ModelToolArgumentsError` 加有界修复（Team Session 和 single runner 都支持）；`StepModelError` 消息带类型链；`bench/` 新增 3 个任务、评分器和运行器（需 `--confirm-live`，默认最多 6 次）；DECISIONS D-012、D-013。
- **Verification run**：新增测试 20 个通过；init.ps1 PASS（30 passed）；历史 tests/agent（git 9138ac1 → TEMP）450 passed。没有调用真实模型。
- **Evidence recorded**：[capability-phase1-2026-11-04.md](docs/harness/capability-phase1-2026-11-04.md)，feature_list.json → capability-phase1-001
- **Commits**：无
- **Known risks**：修复对真实模型是否有效尚未验证；前端页面只做静态评分；AST 守卫不是沙箱
- **Next best action**：经用户授权后跑一轮评测（6 次运行），和 frontend-compare-001 的 0/1 对比

### 2026-11-04 — frontend-compare-001：single 与 team 真实模型对照（前端页面）

- **Goal**：用户授权，用 single 和 team 各跑一次真实复杂代码任务（单文件 HTML 待办页面）。
- **Completed**：在 TEMP 沙箱放同一份 spec；两种模式各跑 1 次（只读，web search 关闭，默认上限，不重试）。
- **Verification run**：single failed（StepModelError，2 次执行，37s）；team failed（TeamError → ModelResponseError → JSONDecodeError，7 次模型调用，45s）。严格交付 0/1 对 0/1；没有 HTML，所以浏览器验收无对象。
- **Evidence recorded**：[frontend-compare-2026-11-04.md](docs/harness/frontend-compare-2026-11-04.md)，feature_list.json → frontend-compare-001
- **Commits**：无
- **Known risks**：样本量 1；single 的具体失败子类无法从 CLI 得知
- **Next best action**：如果授权，先让失败类型可诊断，或开放受限 write-root 后再对照

### 2026-11-04 — team-audit-001：Agent Team V1 文档—代码一致性审查

- **Goal**：读 Team 源码，核对文档，按 harness 管理，测完及时清理。
- **Completed**：逐条核对 16 条声明（15 条一致，1 条表述不精确已更正，4 处未写明的细节已补充）；新增 tests/test_team_contracts.py；init.ps1 自动清理 basetemp；DECISIONS D-011；更新总交接 §7、质量文档、清单、索引。
- **Verification run**：历史 Team 测试 68 passed、路由测试 42 passed（git 9138ac1 → TEMP，已删除）；init.ps1 PASS，pytest 10 passed。
- **Evidence recorded**：[team-audit-2026-11-04.md](docs/harness/team-audit-2026-11-04.md)，feature_list.json → team-audit-001
- **Commits**：无（等待用户确认）
- **Known risks**：Team 真实交付质量没有改善（0/3）；仓库内的 Team 测试只覆盖契约层
- **Next best action**：确认是否提交；如果授权，开始 team-delivery-reliability 的第一个子项

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
