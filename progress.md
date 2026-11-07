# Aletheia Progress

格式按课程模板 claude-progress.md。状态以 feature_list.json 和 session-handoff.md 为准；历史详情见 docs。

## Current Verified State

- **Repository root**：`D:\tries\agent`（分支 main；HEAD 是 harness 提交，父提交 `9138ac1` 为 Team V1 冻结点；工作区有用户选择暂不提交的改动）
- **Standard startup path**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`；运行 Agent 用 `.venv/Scripts/python.exe -m app.agent <goal> --filesystem-root . --no-knowledge`（需真实模型）
- **Standard verification path**：init.ps1（static/import + pytest `tests/`，2026-11-06 为 1466 passed、16 skipped；历史测试已恢复进 `tests/`，见 D-030）；基线核对命令见 docs/harness/baseline.md。原 1093 个测试仍未恢复
- **Highest priority unfinished feature**：backlog `team-delivery-reliability`（authorized=false）；部分完成项 `longterm-integration-001`
- **Current blocker**：没有阻塞交接的项。限制：测试覆盖很窄；用户的源码/README/删除项按用户选择暂不提交（记录在基线清单中）；外部备份在本会话无法访问

## 已确认基线（历史，未重新测量）

- 本地 HEAD 9138ac1 是 Agent Team V1 冻结点；普通执行仍是默认，Team 需显式 opt-in。
- 三组真实对照：普通严格交付 2/3，Team 0/3，不能得出准确率优势结论。
- 清理前回归 1093 passed / 2 skipped；之后 tests 和验证脚本按用户要求删除。
- 长期记忆接入代码和用户注释未提交，保留在工作区；没有默认装配，也没有完整验收结论。

## Session Record

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
