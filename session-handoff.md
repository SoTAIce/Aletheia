# Current Session Handoff — 2026-11-12

## 新会话从这里开始（2026-11-12，第 4 阶段 4c 已并入 main）

**这些交接文件现在在哪里**
- 用户 2026-11-12 决定：交接文件只用于 Agent 之间（两个 Claude 账号、Codex）交接，不进 `main`。`session-handoff.md`、`progress.md`、`feature_list.json`、`docs/harness/baseline.md`、`docs/harness/history/` 只在 `handoff` 分支上（与 `main` 没有共同历史，路径不变；D-077）。
- 在仓库根目录检出到 `.handoff/`（`main` 的 `.gitignore` 已忽略）：第一次运行 `git fetch origin handoff` 和 `git worktree add .handoff handoff`，以后在 `.handoff/` 里 `git pull`。收尾时在 `.handoff/` 里提交，按授权推送 `handoff` 分支。**不要把这些文件提交到 `main`**。
- 下面各节里指向 `docs/...` 计划文档的链接在 `main` 上，不在本分支。

**当前状态**
- `main`（已推送到 `origin/main`）：`70f7470` 4b → `25aa47c` 4c → `2a5ab26` 删除 `agent_planning_timeout_seconds` → `e6973f3` 交接文件移出 `main`（D-077）。远端分支 `stage4c` 已经全部并入 `main`；重复做 4b 的 `claude/wizardly-ride-peavwg` 本地已删除；远端删除被云端会话的权限检查拦下，待用户在 GitHub 上手动删除。
- 4c 留出集复跑（2026-11-12，在云端会话跑，设置同用户本机 `.env`）：单一循环 12/13，达到 ≥10/13。唯一失败 `side_effects_qa`（`mutates_arguments`，历来都错）。3 道问答题第一次请求约 6,400 提示令牌（少开销状态），其余约 2 万的全部通过。详见 `main` 上 `docs/stage4_plan.md` 第 14 节“4c 留出集复跑”。
- Linux 下 pytest：1722 通过、22 跳过、4 失败（只在 Windows 上成立的路径断言，改动前同样失败）。`init.ps1` 本身没在 Windows 上跑过，用户本机拉取后跑一次。
- 用户本机拉 `main` 时：`session-handoff.md`、`progress.md`、`feature_list.json` 和 `docs/harness/` 的基线文件会被删除。若本机这些文件有未提交修改，先另存。用户自己的未提交差异（`scripts/` 下三个脚本的删除）不受影响。

**服务商状态检测（2026-11-12 同一会话，最后做的）**
- 用户在第 4 阶段结束后选了这个方向。方案和实现记录：`docs/provider_state_plan.md`，D-079。
- 查明：我们的请求约 2,500 令牌；中转站平时加约 1.75 万隐藏内容，有时加约 4 千，有时报告的比发出的还少；请求 `gpt-5.6-luna`，响应都是 `gpt-6.1-sol`。代码题只在加大量内容时通过。
- 已实现：每次调用按“报告值 ÷ 本机估算”分档，记进用量、轨迹、评测；评测按档分组；命令行和网页只在异常时提示一行（目前中转站每次都会触发“模型不同”的提示，属如实反映）。
- 提交 `01e2294`、`af3efba`、`e39de77`，2026-11-12 经用户同意已推送到 `origin/main`。
- 没做（用户决定）：评测自动重跑、DashScope 直连对照。

**4d（2026-11-12 同一会话，后加）**
- 方案 `docs/stage4d_plan.md`。用户：暂时没有第二个模型（以后可能 DeepSeek、Qwen、Claude，先不用）；问题 2～4 按建议；Claude 实现。
- 已做：4d-1 模型档案结构（`app/models/model_profile.py`，`Settings.agent_model_profile`，请求逐字节不变），4d-3 每次模型调用前检查预算（`StepBudgetError`）。D-078。提交 `e54d849`、`948604f`、`fc13b4d`。
- 2026-11-12 经用户确认（“可以将这三个上传了”）已推送到 `origin/main`。
- 以后加第二个模型时做 4d-2（`models.toml`，角色 `route`/`member`/`explorer`）和 4d-4 评测；Claude 的接法到时另定（方案第 7 节后的说明）。

**第 4 阶段剩下的（4d 之前写的，第 2 项已由 4d-3 完成）**
1. 4d 模型档案：推迟。
2. 循环一步内多次模型调用之间检查令牌预算：暂不做，等 4d 一并考虑（D-076、D-077）。
3. `SessionService` 单独成类：暂不做。
4. `scripts/validate_multi_runner_live.py`、`scripts/validate_summary_context.py` 还按已删除的规划模式写，已不能运行；用户本机这两个文件本来是删除状态，没动。
5. 用户本机 `.env` 里的 `AGENT_PLANNING_TIMEOUT_SECONDS=120` 已无代码读取，留着无害，删不删由用户决定。

**云端会话的经验**
- 用户的个人云端环境 `Aletheia` 存了中转站 `happycodeapi.com` 的密钥（代理注入，环境变量里填占位值 `AGENT_MODEL_API_KEY=proxy-injected`）。评测要的其他设置照用户 `.env` 用环境变量给出。云端没有 PowerShell 和 torch，测试不需要 torch。


## 上一轮交接（2026-11-11，第 4 阶段 4b、4c）

**当前状态**
- `main` 已推送到 `origin/main`（2026-11-11，用户“帮我上传GitHub”，到 `14cd9ca` 重写 README）；4b 随后在本地提交，未推送。
- 4b（stage4b-001）已实现、已审查，用户“提交 4b”后已提交：
  - 循环和团队任务改在 `app/agent/loop_run.py` 的 `LoopRun` 上运行，状态放在 `app/models/run_state.py` 的 `RunState`/`RunMemory` 里；规划模式仍走 `AgentRuntime`，到 4c 再删；
  - 接线在 `app/services/run_assembler.py`（`loop_run_factory`）和 `app/services/rag_agent_service.py`（`_open_task`、`_execute`）；
  - 新测试：`tests/test_run_state.py`、`tests/test_loop_run.py`（14 项）、`tests/test_loop_run_equivalence.py`（67 项新旧路径对照，以旧代码为准）；改了 4 项接线测试（`test_delegation.py`、`test_route_selection.py`、`test_run_options.py`、`test_web_loop_runner.py`），让替身同时顶替 `LoopRun`；
  - init.ps1 通过：2084 项通过、16 项跳过。
- 4b 评测（方案 [stage4_plan.md](docs/stage4_plan.md) 第 11 节和 11.1 节）：
  - 调参集单一循环 9/11、团队 9/11；
  - 重跑两题后合并计算，单一循环 11/11、团队 10/11（3b 是 10/11 和 11/11）；团队 `todo_html` 仍错在 `enter_key_and_trim`，是这道题本身的波动；
  - 看不出回退。原先“失败是因为提示开销缺失”的判断不成立，已在 11.1 更正。
- 本轮暂存的文件要排除用户自己的差异（见下面“用户自己的差异”）。4b 的提交范围是上面列的代码和测试文件，加上 `docs/stage4_plan.md`、`feature_list.json`、`progress.md`、`session-handoff.md`。`run_assembler.py` 和 `rag_agent_service.py` 不属于用户，可以整文件暂存。
- （4c-3 后已作废）原先要求提交前核对用户三个文件的哈希；这三个文件已按用户确认删除，最后的哈希记在新基线清单里：
  - `planner.py`：`a2ce0f91c476ea47`
  - `step_executor.py`：`86f8ca77ebd86d96`
  - `tool_calling_step_runner.py`：`f61016aabfffe6bd`

**剩下的任务（用户 2026-11-11 说“剩下任务稍后再做”）**
1. ~~问用户是否提交 4b~~（已提交）。
2. 4c：删除规划模式和规划—执行骨架。详细设计和测试清单已写成方案第 12、13 节（stage4c-001，partial）。用户同意测试清单，选择由 Claude 现在实现（Codex 额度 11 月 15 日恢复），联网搜索演示改写成循环。4c-1 到 4c-4 已完成（方案第 14 节，D-076，未提交；用户已确认三份笔记保存好，三个文件已删除）。评测已跑（调参集单一循环 9/11、团队 10/11、留出集 6/13，原因分析见方案第 14 节“4c 评测”）。用户决定今天先停在 4c；剩下：是否在服务商状态正常时重跑留出集、提交 4c（要用户授权）、推送（另行授权）。第 4 阶段其余未决项：4d 模型档案（推迟）、循环一步内是否逐次检查令牌预算、`SessionService` 是否单独成类、无人读取的 `agent_planning_timeout_seconds` 是否删除。用户自己的差异现在只剩 `scripts/` 下三个脚本的删除；基线清单已刷新为 `docs/harness/history/baseline-2026-11-11-stage4c.json`。动手之前：
   - 用户要先自己保存 `planner.py`、`step_executor.py`、`tool_calling_step_runner.py` 三份中文笔记，确认保存好了才能删；
   - 先把要删、要改的测试清单给用户看，用户同意后再动；
   - 同时改写 `AGENTS.md` 第一条不变量（用户已同意），顺带删掉 `StepRoutingError`；
   - 先写 4c 详细设计（方案新一节），再由 Codex 实现。
3. 4d：模型档案，暂时推迟。
4. 4b 的提交还没推送，推送要用户另外授权。

**Codex 的用法（本轮经验）**
- 用 Agent 工具派 `codex:codex-rescue`，任务说明开头加 `--background --fresh`，不加 `--model`/`--effort`（读 `~/.codex/config.toml`）。
- 任务拆小（4b-1、4b-2 分别 8 和 12 分钟完成）；派出后挂监视：任务结束，或日志 20 分钟不更新就通知；它曾两次卡在 `node_repl`。取消要在 PowerShell 里做。

## 上一轮交接（2026-11-10，选择工作目录已完成并并入 main）

**当前状态**
- `main` 已包含追踪（trace-001，`db13d64`，D-073）和选择工作目录（workspace-001，D-074）。工作目录在实验分支 `exp/workspace-choice` 上做完，用户在网页上用真实模型试过并提了两条意见（要系统弹窗选文件夹；工作树里看不到以前的会话），修完后快进并入 `main`。2026-11-10 经用户授权已推送到 `origin/main`（到 `ae5440b`，用户自己的未提交差异不在其中）。
- 实验工作树和分支 `exp/workspace-choice` 已于 2026-11-10 删除（先拆掉指向主目录 `.venv` 的目录链接，主目录环境完好）。残留的空文件夹 `.claude/worktrees/workspace-choice` 已于 2026-11-10 删除。**以后删工作树前先拆 `.venv` 链接**（`cmd /c rmdir <工作树>\.venv`），不要连同链接一起递归删除。
- `feature_list.json` 的 active_feature=null。init.ps1 通过：1807 项通过、16 项跳过（2026-11-10，第 3 阶段 3b 之后）。基线清单 [baseline-2026-11-09-kb3.json](docs/harness/history/baseline-2026-11-09-kb3.json)，本轮未刷新。
- 知识库：Milvus 集合 `biz` 里 61 个来源、844 块。Milvus **不会自启**；重启电脑后先开 Docker Desktop，再运行 `docker compose -f deploy/milvus/compose.yaml up -d`，见 common.md。

**选择工作目录：现在是什么样（详见 [方案](docs/workspace_choice_plan.md) 第 10、11 节和 D-074）**
- 所有目录（网页选的、`/dir` 选的、启动参数给的）都过 `app/services/workspace.py` 的同一套检查；系统盘（C 盘）目录必须确认：网页弹确认框、服务端不确认返回 409、`/dir` 输入 `y`、启动参数交互确认或 `--confirm-system-drive`。
- 禁止读写的目录：`.env` 的 `WORKSPACE_DENY_DIRS` 加本项目 `data` 和对话归档目录；在工作目录里面时文件工具和沙盒都看不到。
- 网页：输入框下的文件夹按钮 → 目录对话框，可手填路径，也可点“浏览…”弹出系统的选择文件夹窗口（`app/services/folder_picker.py`，服务端在本机子进程里用 tkinter 打开，只在本机地址提供，一次一个）。可拖入 UTF-8 文本文件作附件（每条最多 5 个、每个最多 100 万字符，存在对话归档里，模型用 `read_context_artifact` 读）。
- 每次运行按选中目录启动文件和沙盒服务；不选目录时行为和以前一样。`WORKSPACE_CHOICE=false` 整体关闭。
- 已知限制：GBK 等非 UTF-8 文本和图片不能当附件；网页上没有目录浏览器（只有系统弹窗）；真实系统弹窗只在用户屏幕上试过，自动测试用的是替身。

**用户自己的差异，永远不进提交**
- `app/agent/planner.py`、`app/agent/step_executor.py`、`app/agent/tool_calling_step_runner.py` 里的中文注释；删除的 `scripts/benchmark_conversation_memory.py`、`scripts/validate_multi_runner_live.py`、`scripts/validate_summary_context.py`。改这三个代码文件时只能暂存自己的那几处（`git hash-object -w` 加 `git update-index --cacheinfo`），不能整文件暂存。

**工作方式**
- 只用中文回复，不中英夹杂（路径、命令、代码名除外）。
- Codex 有额度时：Claude 写方案（`docs/*.md`）并登记，用 Agent 工具 `codex:codex-rescue` 派它实现（不加 `--model`/`--effort`，读用户配置），完成后由 Claude 审查。额度用完或失败时告诉用户，由 Claude 自己做。小改动、记录和只读检查由 Claude 直接做。
- 较大的改动先用工作树开实验分支（`exp/...`），用户试过、确认成功后再并入 `main`（用户 2026-11-10 的要求）。
- 不推送、不 amend；真实模型、评测和嵌入接口每次都要用户明确授权。
- 从工作树启动网页时，工作树里没有 `.env`：先把主目录 `.env` 读进当前终端再启动（命令见 common.md 或上一轮记录），对话记录默认存在工作树自己的 `data` 里；要看到以前的会话，设 `CONVERSATION_STORAGE_DIR=D:\tries\agent\data\conversations`。

**下一阶段可选任务（都要用户决定后才做）**
1. **工作目录的后续**：GBK 和图片附件；网页里的目录浏览器（需要一个列目录的接口，要另想安全边界）。
2. **追踪的后续**：~~用户在真实网页上点一次“执行记录”确认~~（2026-11-10 已完成：用户用真实模型在 8001 端口的网页上发了一条消息，点开执行记录显示正常；审计文件用 `python -m app.agent.trace --json` 解析为版本 2、记录完整、丢弃 0，2 次模型调用、1 次工具调用、约 4.6 万输入令牌、120 秒）；授权一次带 `--keep-traces` 的评测；记录原文、OpenTelemetry、实时追踪见 [追踪方案](docs/tracing_plan.md) 第 8 节。
3. ~~小问题~~（2026-11-10 status-cleanup-001 已完成，未提交）：`--prune` 磁盘根前缀已修；嵌入服务的 `ValueError` 经核对保持现状（依赖在请求中抛的 `ValueError` 归为请求失败，这是正确分类），补测试固定；`--exclude` 的 `/absolute`、单反斜杠绝对路径已有专门测试；功能表里长期记忆的两条过时登记已加说明。空文件夹 `.claude/worktrees/workspace-choice` 已删除（用户同意后结束了占用它的旧网页服务，进程 86428）。
   另两处遗留（status-cleanup-002，已完成）：`VectorStoreManager.list_documents`（关键词检索启动时全量读取）改用查询迭代器，不再依赖含已删记录的 `num_entities` 和 16384 的 offset 窗口；`index_directory` 的失败日志只记异常类型。剩下的只有设计限制：建库与清理并发无事务保证。
4. **架构第 3 阶段**：方案已写成 [stage3_team_delegate_plan.md](docs/stage3_team_delegate_plan.md)（stage3-plan-001，2026-11-10），用户“都按建议”；团队基线 7/11（问答题被旧完成条件误拦，见方案 10.1）；3a 已实现并审查（stage3-team-loop-001，init.ps1 1851 通过），3a 评测：团队新实现 9/11（基线 7/11，代码类同为 6/7），单一循环 10/11，达到进入 3b 的标准（方案 10.2）；3a 已提交 `cefcd2d`；3b 已完成未提交（方案 9.2、D-075，init.ps1 1807 通过），3b 评测团队 11/11、单一循环 10/11（方案 10.3），第 3 阶段达标；3b 与 `AGENTS.md` 两条不变量的改写已提交 `bab704a`（未推送）。下一步候选：第 4 阶段（精简任务状态、拆服务层、删规划—执行骨架，届时一并删 `StepRoutingError`）。
5. **单一循环按需委派**：方案 [loop_delegate_plan.md](docs/loop_delegate_plan.md)（loop-delegate-plan-001），用户“都按建议”；已实现（loop-delegate-001，方案第 10 节；Codex 卡住后由 Claude 完成），init.ps1 1827 通过，未提交。评测已跑（方案第 11 节）：调参集开委派 10/11、令牌 0.84 倍、0 次委派；探针题不开委派也都答对，开了反而贵 6 倍。建议默认关闭、不替代 `auto` 和团队模式；要继续先找真正需要委派的题。已提交 `a0aa489`。
6. **架构第 4 阶段**：方案 [stage4_plan.md](docs/stage4_plan.md)（stage4-plan-001，2026-11-11）：4a 拆服务层 → 4b 循环脱离骨架 → 4c 删除规划模式和骨架（会删掉用户写了中文笔记的 `planner.py`、`step_executor.py`、`tool_calling_step_runner.py`，并改写 `AGENTS.md` 第一条不变量）→ 4d 模型档案推迟。用户“都按建议”（做 4c 前用户自己先保存三份笔记）。4a 已提交 `e6a9856`（stage4a-001，方案第 8 节，init.ps1 1833 通过）。4b 详细设计已写成方案第 9 节（stage4b-001），分 4b-1 `run_state.py`、4b-2 `loop_run.py` 加新旧路径对照测试、4b-3 接线三块；已实现（方案第 10 节，init.ps1 2084 通过），未提交。评测（方案第 11 节）：单一循环 9/11、团队 9/11；重跑两题后合并为单一循环 11/11、团队 10/11（团队 `todo_html` 仍错在 `enter_key_and_trim`，属于这道题的波动）。原先“失败因提示开销缺失”的判断不成立，已在 11.1 更正。下一步 4c（开工前用户先保存三份笔记）。

**用户已决定不做的**（2026-11-09：“agent 可以解决正常问题就可以”）
- `side_effects_qa` 不再追：标准答案认为 `remember(item, history=[])` 只改默认参数、不算修改传入参数，这是题目对边界情形的定义之争。
- 中转站疑似换上游（个别请求的令牌数异常）暂时不解决。

## 最新交接：kb-index-cli-003（已提交 `64b2a49`，真实建库完成）

- **Live verified**：`--exclude harness/history --confirm --prune`：61 个文件全部成功、844 块，失败 0；清掉 `harness/history/` 下 7 个来源共 28 块；知识库与按排除后的文件列表重新切块的结果逐条相同。
- **以后建库**：用 `.venv\Scripts\python.exe -B -m app.knowledge docs --exclude harness/history --confirm --prune`。
- **Next best action**：网页上用真实模型验证单一循环，再决定默认改成循环。

## kb-index-cli-003 实现与审查记录

- **修改与验收**：按 docs/kb_index_v3_plan.md 统一限制 8000 UTF-8 字节，超长块再切保留元数据和顺序；增加可重复 --exclude、路径段过滤、参数错误返回 2 和排除计数。帮助和 common.md 明确说明 --prune 会清理排除文件的已有来源；D-071 记录 JSON 片段与路径边界。
- **当前验证**：新增 23 项确定性测试；针对性 86 项通过（3.93 秒）。init.ps1 管理状态、94 个 Python 文件语法、CLI 加载、1720 项通过、16 项跳过（53.63 秒），退出码 0；结束复核再次通过：1720 项通过、16 项跳过（53.40 秒）；两次最新预演为 70 个文件/984 块、61 个文件/844 块（排除 9 个），均退出码 0；对应结束清单最终哈希刷新前快照。原问题 JSON 已切为 104 块，离线审计无超限且服务或网络调用尝试为 0。
- **独立审查与限制**：无严重或重要问题；轻微建议是补 /absolute 及单反斜杠绝对路径专门回归，当前逻辑拒绝但矩阵未单列，已延后。初次并行普通预演遇 WinError 1455，顺序重跑成功，未改依赖或系统设置。既有磁盘根目录清理前缀等问题不扩展。
- **保护与基线**：HEAD 44d435c，用户 README、三个中文注释文件与三个脚本删除项保留；kb2 实测记录、原方案及登记保留。未暂存、还原、提交、amend 或推送。结束清单 docs/harness/history/baseline-2026-11-09-kb3.json 保留启动快照及独立保护哈希；命令和四项基线结果见 progress.md、docs/harness/baseline.md。
- **下一步**：待审查后由用户另行授权真实建库与排除来源清理。本轮未连接真实 Milvus、调用嵌入或真实模型，未运行实际 --confirm/--prune，不据离线通过声称真实写入或清理成功。

## 本轮开工交接：kb-index-cli-003（原方案，保留）

- [建库命令：超长块与排除方案](docs/kb_index_v3_plan.md)：超长块再切（按 UTF-8 字节，上限 8000）；`--exclude` 按相对路径排除。要改的文件里没有用户的未提交改动。本轮清单 [baseline-2026-11-09-kb3.json](docs/harness/history/baseline-2026-11-09-kb3.json)，以 HEAD `44d435c` 为起点。

## 上一项交接：kb-index-cli-002（完成，已提交 `44d435c`，真实验证完成）

- **Live verified**：--prune 预演只读，0 个过时来源；--confirm --prune：68 个文件成功 67 个、写入 859 块；建库后每个来源的块数都等于现在的切块结果，没有重复或残留，没有误删。失败的 1 个是 `docs/harness/history/2026-11-04-state-before.json`：3 块超过 Milvus 内容字段 8000 字符上限，写入被拒，没有影响其他数据。
- **Next best action**：等用户决定是否修超长块（再切或跳过），以及建库时是否跳过 `docs/harness/history/`。

## kb-index-cli-002 实现与审查记录

- **修改**：按 [建库命令改进方案](docs/kb_index_v2_plan.md) 实现先写后删、五类阶段提示与按阶段停止建议、目录边界内的可选 `--prune`；只读列来源禁止创建或重建集合，新编号不完整时保留旧块（D-070）。
- **当前验证**：新增 48 项确定性测试，连同原有 10 项共 58 项通过（2.59 秒）；init.ps1 通过，94 个 Python 文件、1692 项通过、16 项跳过（55.43 秒）。默认 docs 预演最初 68 个文件、874 块；记录补齐后重跑为 68 个文件、876 块，均退出码 0。同入口网络审计均为 0 次连接或地址解析，第二次计数对应结束清单刷新前的快照。
- **独立审查**：三项重要问题已补回归并修复：来源改用 metadata 查询迭代器，避免实体统计与 offset 窗口问题；命令全过程屏蔽底层日志并恢复原状态；当前来源比较统一 resolve，避免别名误删。磁盘根目录加斜杠的前缀限制延后，使用具体文档子目录。
- **兼容与取舍**：异常子类兼容原基类与消息；预检统一包为 read 阶段，直接捕获 ValueError 的单文件调用方需调整。删除失败时新旧块暂时并存，下次重跑清掉，不承诺事务。（原“日志恢复依赖 Loguru 内部激活状态”已在审查后改掉，见下一条。）
- **命令**：`.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp <系统 TEMP 下本轮唯一目录> tests/test_knowledge_cli.py`；`.venv/Scripts/python.exe -B -m app.knowledge docs`；`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`；基线按 docs/harness/baseline.md 核对。
- **未验证**：真实 Milvus、嵌入接口、真实模型和真实 `--confirm`、`--prune`；下一步是真实验证，待用户授权，不自动运行。
- **保护与状态**：用户 README、三个中文注释文件、三个脚本删除项保留；原方案原样保留；未暂存、还原、提交或推送。本轮清单 [baseline-2026-11-09-kb2.json](docs/harness/history/baseline-2026-11-09-kb2.json)，以 HEAD `7835fa2` 为起点；最终哈希与退出检查见 progress.md。
- **Claude 审查后修改**（用户“修掉两处”）：日志静默移到命令行入口 `main()`，只用 Loguru 公开的 `disable`、`enable`，不再读写 `logger._core`；`connect()` 把连接阶段和集合阶段分开，维度不一致、只读时集合不存在改抛 `MilvusCollectionError`，命令报“Milvus 集合不可用”，不再误报“连接 Milvus 失败”（D-070 补记）。`tests/test_knowledge_cli.py` 63 项通过，init.ps1 1697 passed、16 skipped，`docs/` 预演 68 个文件、881 块。

## 上一项交接：2026-11-08 tool-list-dedup-001（完成，已提交）

- **Changes**：执行步骤序列化上下文时去掉钉住的工具清单，共用常量 `step_models.TOOL_LIST_SOURCE`；规划器、团队负责人、任务状态不变（D-069）。代码和测试由 Codex 写，它在写记录前额度用完，审查、测量和记录由 Claude 完成。
- **Currently verified**：init.ps1 PASS，94 个 Python 文件，1644 passed、16 skipped；离线步骤上下文 3,262 → 1,495 字符；`tool_calling_step_runner.py` 退掉两处预期改动后与用户版本哈希一致。
- **Live verified**：调参集规划模式各 1 次：11/11，和改动前相同；调用次数相同的题提示令牌都少了，剔除受中转站额外令牌影响的 `config_merge_qa` 后少 5.8%；报告 `output/bench-tune-plan-nodup.json`。feature 状态 done。
- **提交注意**：`tool_calling_step_runner.py` 混有用户未提交的中文注释，提交时只能提交本轮的导入和两行过滤，不能整文件暂存。
- **提交**：2026-11-09 经用户授权提交，`tool_calling_step_runner.py` 只提交了导入和两行过滤，用户的中文注释仍未提交；未推送。
- **Next best action**：建库命令改进（kb-index-cli-002，交给 Codex）；之后是网页上用真实模型验证单一循环，再决定默认改成循环。

## 最新交接：2026-11-08 deps-cleanup-001、kb-index-cli-001（完成，已提交）

- **Changes**：`pyproject.toml` 去掉 `langchain-mcp-adapters`；`langgraph` 的 4 个固定版本保留，因为 `langchain` 1.x 间接需要它们（D-067）。新增建库命令 `python -m app.knowledge 目录`，默认只预演，`--confirm` 才写入 Milvus（D-068）。
- **Currently verified**：init.ps1 PASS，94 个 Python 文件，1639 passed、16 skipped；`tests/test_knowledge_cli.py` 10 项通过；对 `docs/` 实际预演 64 个文件、836 块，没有连接任何服务。
- **Still unverified**：`--confirm` 没有对真实 Milvus 和嵌入接口跑过；没有卸载 `.venv` 里的 `langchain-mcp-adapters`。
- **Next best action**：要真正建库，需要先启动 Milvus，并在 `.env` 里配好 `DASHSCOPE_API_KEY`。
- **Evidence / State**：D-067、D-068；feature_list deps-cleanup-001、kb-index-cli-001 已完成，active_feature=null；本轮清单 [baseline-2026-11-08-deps-kb.json](docs/harness/history/baseline-2026-11-08-deps-kb.json)。以 HEAD `73d2618` 为起点，经用户授权提交，未推送；用户原差异仍未提交。

## 上一项交接：2026-11-08 cleanup-stage0-001（完成，已提交）

- **目标与修改**：从 HEAD `2b76f01` 加用户原差异开始，按 docs/stage0_cleanup_plan.md 删除九个遗留文件，新增七项模块守卫；指定文档、功能表、D-066、进度、测试数和基线已同步。
- **已验证**：init.ps1 通过（92 个 Python 文件，1629 passed、16 skipped，50.69s）；新增守卫删除前 4 项失败、3 项通过，删除后连同两个保留示例测试共 15 passed（2.74s），没有跳过。只使用确定性测试、本地测试子进程与模拟 HTTP/模型，没有调用外部真实服务或真实模型。
- **保留与范围**：time_server.py 和 web_search_client.py 有测试及运行入口；vector_index_service.py 是唯一知识库建库代码，原样保留。mcp_demo 不新增 __init__.py；在用源码、依赖、锁文件、配置、模型默认值及 data/ 不变。用户 README、三处中文注释的文件哈希与启动一致，三个脚本的删除保留，未暂存或还原用户文件。
- **执行差异**：git rm 因 .git/index.lock 写权限被拒绝，改用文件系统删除已授权的同样九个文件，空 aiops/ 已移除，索引不变；没有提交或推送。详见 D-066。
- **证据与状态**：feature_list.json 的 cleanup-stage0-001=done、active_feature=null；progress.md 有命令、先失败后通过及退出检查。本轮清单为 [baseline-2026-11-08-cleanup-stage0.json](docs/harness/history/baseline-2026-11-08-cleanup-stage0.json)，含九项本轮删除和三项用户删除；旧清单保持历史原样。
- **审查与提交**：Claude 独立复核无阻断问题，改正文档索引里网页方案的过时状态和 `docs/agent_runtime.md` 里的中英混杂句；按用户“修完再提交”提交，未推送，用户原差异仍未提交。
- **尚未验证**：真实模型、外部真实服务和真实容器；确定性通过不代表网页真实验收或质量提升。规划模式重复工具清单未改，需要另行授权真实评测。
- **下一步（待用户决定）**：是否从 pyproject.toml 移除源码已不用的 langgraph、langgraph-checkpoint、langgraph-prebuilt、langgraph-sdk、langchain-mcp-adapters；是否给知识库增加建库命令。本轮不自动推进这些工作。
- **命令**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`；单独测试命令见 progress.md；基线核对命令见 docs/harness/baseline.md，已核对四项为 base ancestor OK、drift none、deleted-restored none、uncommitted-outside-manifest none。

## 最新交接：2026-11-08 loop-web-001（完成，已提交）

- **Goal / Changes**：按 docs/loop_web_plan.md 实现网页启动默认与逐消息 runner 选择；服务/API/启动器/中文页面已接入，默认仍 plan。route 事件只在 options 中增加 runner。
- **Currently verified**：原 init.ps1 通过，1622 passed、16 skipped（55.55s）；针对性 111 passed；node --check 通过，独立只读审查无问题。新测试先 RED 再实现，恢复自定义注册表场景另有 RED→GREEN。
- **Restore / Scope**：恢复沿用原 task_options 的 runner；loop 保留原消息及工具结果；自定义注册表只提供 plan，新任务 loop 拒绝。原 README、三处中文注释和三项脚本删除保留。
- **Still broken or unverified**：没有本轮确定性验收失败；没有浏览器/真实模型/真实容器验证，不据此称网页人工确认真实验收通过。沙箱 TEMP 权限失败已在未修改源码上复现；有效环境使用提升权限运行相同检查，未改 ACL 或依赖。
- **Evidence / State**：[验收记录](docs/harness/loop-web-2026-11-08.md)、D-065；feature_list loop-web-001=done，active_feature=null；[本轮基线](docs/harness/history/baseline-2026-11-08-loop-web.json)。实现时 HEAD 为 c15ffd9；随后经用户授权（“先提交”）与第 0 阶段方案一起提交，未推送，用户原差异仍未提交。
- **审查后修复**：`app.js` 兼容不返回 `runners` 的旧版服务（按只有“按计划”处理，不发送 `runner`），单一循环的进度文字不再显示“正在规划步骤”；`node --check` 与 init.ps1（1622/16）通过，未在浏览器验证。
- **下一项方案**：[第 0 阶段清理方案](docs/stage0_cleanup_plan.md)（cleanup-stage0-plan-001），打算交给 Codex 实现 cleanup-stage0-001；loop-web-001 和本方案已一起提交。
- **Next best action**：等待下一项任务；按计划 §7，网页真实模型的问答/人工确认允许与拒绝/ask_user 恢复需另行授权，之后才讨论默认改 loop。本轮不自动推进。
- **Commands**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`；`node --check app/api/static/app.js`；基线命令见 docs/harness/baseline.md。

下文是此前阶段交接，时间和“未提交/未实现”按当时解释；本轮状态以上段为准。

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
