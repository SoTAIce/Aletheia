# 定时任务，和两项前置：网页任务计量、阻塞任务过期（方案）

日期：2026-11-25。状态：**已实施，未用真实模型验证**（甲、乙 D-101，丙 D-102）。第 8 节八条按推荐定（用户 2026-11-25）。分两次由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施：先甲、乙，再丙；Claude 写实施说明并核对，Gemini 结束前清理无关文件。

来源：3.5 排序第 4 项（`docs/batch_run_plan.md` 第 1 节），想法出自用户的 `docs/agent_feature_ideas.md` 4.1 节，前置是设计审查 `docs/agent_design_review.md` 2.6、2.7 节。下面的现状按当前代码（`764c3b6`）核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有用户自己没提交的改动。

想要的效果：在网页后端开着的时候，“每个工作日 9 点总结一下这个目录里新增的日志”“每周一汇总上周的变更”这类只读任务到点自己跑，结果进一个专门的会话，打开网页就能看到。

## 1. 要解决什么

- **甲：网页没有任务级计量**（审查 2.6）：命令行和批处理在每次运行外面套了 `RunMeter`，网页没有，于是 `LoopRun` 自己新建一个不限额的计量器（`app/agent/loop_run.py:209`）。结果：
  - 选执行模式的那次模型调用发生在 `LoopRun` 之前，网页上不进用量、不进对话令牌上限；
  - 网页设不了单个任务的令牌和时间预算。无人值守的定时任务必须有预算，所以这是前置。
- **乙：阻塞的任务永不过期**（审查 2.7）：`ask_user` 阻塞的任务连同整份消息留在 `RagAgentService._tasks` 里（`rag_agent_service.py:896-897`），只有续跑结束或失败时才清掉（`_forget_task`，第 555 行）。用户不回答就离开，这些任务一直占内存。
- **丙：没有定时任务**。

## 2. 现状（已核实）

- **网页运行**：`app/api/runs.py` 的 `RunManager.start` 管同一请求编号去重、每个会话一个运行、并发上限；`_execute` 组装选项后调用 `service.query_result(...)`（第 227 行），外面没有计量器。
- **计量器**：`RunMeter(max_total_tokens=..., timeout_seconds=...)`，`metering(meter)` 挂到上下文变量上；`LoopRun._meter_scope` 有外层计量器就用外层的（D-034）。批处理的写法在 `app/agent/batch.py:505-521`。
- **请求去重**：`query_result(request_id=...)` 有归档时会回放同一编号已有的结果、拒绝内容冲突的重复（`_replay`），进程重启后也成立。
- **不追问**：`query_result(allow_ask_user=False)`（D-097 加的），模型拿不到 `ask_user`，缺信息时在答案里说明。
- **只读**：`allow_writes=False` 时看不到写入和运行类工具（D-027）。
- **启动确认的现成写法**：用户 MCP 服务（D-095）按指纹存在 `data/mcp_confirmed.json`，没确认过的要在终端回答 `y` 或带 `--confirm-mcp`（`app/tools/user_mcp.py:373` 的 `confirm_user_mcp`）。
- **工作目录检查**：`WorkspacePolicy.check`（`app/services/workspace.py:114`）。
- **网页确认**：没人在页面上时，确认卡片等 `WEB_APPROVAL_TIMEOUT_SECONDS`（默认 300 秒）后按拒绝处理（D-061）。

## 3. 设计

分三块实施，顺序是甲、乙、丙；甲、乙各自独立有用，丙依赖甲。

### 3.1 甲：网页每次运行一个计量器（第 8 节第 1 问）

- `RunManager._execute` 在调用 `query_result` 外面套 `metering(RunMeter(max_total_tokens=..., timeout_seconds=...))`，和命令行一样：选执行模式的调用、对话令牌上限、用量全都按这一次运行算。
- 新设置 `WEB_RUN_MAX_TOKENS`、`WEB_RUN_MAX_SECONDS`，默认都是不限，所以不配置时只有用量里多了选模式那次调用，其他行为不变。
- 超了预算时任务按 `budget` 失败，网页照现有失败事件显示。
- 续跑是新的一次运行，新的计量器（和命令行一致）。

### 3.2 乙：阻塞任务过期（第 8 节第 2 问）

- 阻塞的任务记下进入阻塞的时间。新设置 `BLOCKED_TASK_TTL_SECONDS`，默认 86400（24 小时）。
- 每次 `query_result`、`resumable` 时顺手清理过期的（不另开后台任务）：调用 `_forget_task`。
- 过期后 `resumable` 返回假，网页照现有的说明“这个任务已经不能接着回答了，请重新提问”；归档里那一轮仍是“阻塞”，不改写。
- 命令行交互模式同样生效（进程开着超过 24 小时才会碰到）。
- 不做：会话锁字典 `_locks` 和会话对象的回收（另一件事，会话对象本身也一直留着）。

### 3.3 丙：定时任务

#### 配置（第 8 节第 3、4 问）

- 文件 `schedules.toml`（设置 `SCHEDULES_FILE`，模板 `schedules.example.toml`），读法照 `permissions.toml`：默认文件不存在就没有定时任务；明确指定了却找不到，或者文件有错，就拒绝启动（中文说明，退出码 2）。
- 每个 `[[schedule]]`：
  - `name`：字母、数字、`-`、`_`，1～40 个字符，不能重复；
  - `at`：`"HH:MM"`，本机时区；
  - `days`：`"daily"`、`"weekdays"`，或星期列表如 `["mon", "thu"]`；
  - `goal`：要做的事（最多 4000 字符）；
  - `workspace`：可选，工作目录，启动时按 `WorkspacePolicy` 检查，不合格就拒绝启动；不写用网页的默认目录；
  - `max_tokens`：**必填**，正整数；
  - `max_seconds`：可选，默认 300；
  - `enabled`：可选，默认 `true`。
- 不用 cron 表达式：要么自己写解析器，要么加依赖；“每天/工作日/某几天的几点”覆盖想法清单里的例子。
- 最多 20 个。

#### 启动确认（第 8 节第 5 问）

- 无人值守会花钱、会读文件，所以和用户 MCP 服务一样：每个定时任务按内容算指纹，存在 `data/schedules_confirmed.json`；没确认过的，启动时在终端列出（名字、时间、目标前 80 字、目录、预算）问 `y`，或带 `--confirm-schedules`；不确认就拒绝启动。
- `.gitignore` 加 `data/schedules_confirmed.json`。

#### 怎么跑（第 8 节第 6、7 问）

- 只在网页后端里跑（`python -m app.api`），一个调度循环挂在应用的生命周期上，关闭时停下、正在跑的照现有关闭流程处理。命令行和批处理没有调度。
- 每次到点就像网页上发了一条消息，走 `RunManager.start`，所以并发上限、同一会话只跑一个、取消、关闭都和网页消息一样：
  - 会话：每个定时任务一个固定会话 `schedule-<name>`，属于网页用户；每次运行是里面的一轮，历史照常带上（“和上次比”这类任务用得着）；
  - 请求编号：`schedule-<name>-<计划时间 YYYYMMDDHHMM>`，同一时间点只会跑一次，重启后回放不重跑；
  - 选项：单一循环、只读、不用长期记忆、`allow_ask_user=False`、预算来自 `max_tokens` 和 `max_seconds`（经 3.1 的计量器）；
  - 确认：定时运行里需要确认的工具调用**立即按拒绝处理**，不等 300 秒（没人在看）。
- 并发上限或会话正忙时，每 60 秒再试一次，最多试 10 次，还不行就记一条“跳过”。
- 错过的时间点：网页启动时，如果最近一个应该跑的时间点在 12 小时内、还没跑过，就补跑一次；更早的都跳过。运行中电脑睡眠醒来同理。
- 时钟可以注入，测试用假时钟。

#### 记录和显示

- `GET /api/health` 多一项 `schedules`：每个任务的名字、下次时间、上次状态（完成 / 失败 / 跳过 / 还没跑），不带目标原文。
- 网页侧栏会话列表里，`schedule-` 开头的会话带“定时”标签；顶栏显示“定时任务 N 个”。这次不做未读标记。
- 启动时打印一张表：名字、时间、下次运行时间、预算。
- 钩子事件和轨迹照普通运行记录，`route` 之外多一个 `schedule` 字段（任务名和计划时间），不带目标原文。

### 3.4 要改的文件

- 甲：`app/api/runs.py`（`_execute` 套计量器）、`app/config.py`、`.env.example`、`app/agent/run_limits.py`（`--show-limits` 多两行）。
- 乙：`app/services/rag_agent_service.py`（阻塞时间、清理）、`app/config.py`、`.env.example`、`app/agent/run_limits.py`。
- 丙：
  - 新增 `app/api/schedules.py`（配置读取、指纹确认、时间计算、调度循环）、`schedules.example.toml`；
  - 修改 `app/api/__main__.py`（读文件、确认、`--confirm-schedules`）、`app/api/app.py`（生命周期、健康接口）、`app/api/runs.py`（定时运行的入口和立即拒绝的确认）、`app/api/static/app.js`、`index.html`、`app/config.py`、`.gitignore`。
- 新增测试：`tests/test_web_metering.py`、`tests/test_blocked_task_expiry.py`、`tests/test_schedules.py`。
- 不改：循环、执行器、批处理、黄金对照文件、模型默认值、依赖。

## 4. 测试（离线，假模型、假时钟）

- 甲：网页运行的用量里有选模式那次调用；对话令牌上限算上它；设了 `WEB_RUN_MAX_TOKENS` 时超了按 `budget` 失败；不设时其他输出不变。
- 乙：阻塞 24 小时内能续跑；过期后 `resumable` 为假、续跑被拒；过期清理不影响正在跑的任务和其他会话。
- 丙：
  - 配置：各字段的合法和非法写法、重复名字、超过 20 个、目录不合格、`max_tokens` 缺失，都拒绝启动；文件不存在时没有定时任务；
  - 确认：没确认过的要确认、确认过的不再问、内容改了要重新确认、不是终端又没带参数就拒绝；
  - 时间：`daily`、`weekdays`、星期列表的下次时间；跨天、跨周；
  - 到点触发一次；同一时间点重启后不重复（请求编号回放）；12 小时内错过的补跑一次，更早的跳过；
  - 运行选项：只读（看不到写入工具）、没有 `ask_user`、预算生效、需要确认的调用立即拒绝；
  - 并发上限时重试，10 次后记“跳过”；
  - 健康接口的 `schedules` 不带目标原文；关闭时调度循环停下。
- 黄金对照文件不变。

## 5. 不做的

- 写入类定时任务（要一套“预先批准”，以后另做）；
- cron 表达式、时区设置；
- 网页上增删改定时任务（只用配置文件）；
- 未读标记、运行通知（排序第 6 项，可以以后接上）；
- 网页没开时到点运行（要系统计划任务，不在这次）；
- 会话锁和会话对象的回收。

## 6. 以后可以接着做

- 运行通知（排序第 6 项）：定时任务完成时网页提示；
- 定时跑一批：目标改成批处理清单（D-097）；
- 写入类定时任务的预先批准。

## 7. 风险

- **无人值守花钱**：每次都有必填的令牌上限和时限，启动时要确认；但服务商加的隐藏令牌会让按报告值计的预算比估算早用完（审查第 7 节），`max_tokens` 要留余量。
- **定时会话的历史越来越长**：每次运行都带上前面的轮次，最终会占满会话历史的预算、更早的轮被删掉（开了历史压缩就会被摘要）。
- **网页没开就不跑**：错过的只补最近一次。
- **甲改变了网页用量的数字**：选模式那次调用开始计入，`auto` 模式下网页显示的令牌会比以前多一点。
- 都没有用真实模型验证。

## 8. 要用户定的

1. **网页计量**：推荐**每次运行一个计量器，加 `WEB_RUN_MAX_TOKENS`、`WEB_RUN_MAX_SECONDS`，默认不限**。
2. **阻塞过期**：推荐**24 小时，`BLOCKED_TASK_TTL_SECONDS` 可改**，过期后照现有说明请用户重新提问。
3. **时间写法**：推荐**`at` + `days`（每天 / 工作日 / 星期列表）**。另一个做法是 cron 表达式，更灵活但要加依赖或自己写解析器。
4. **范围**：推荐**第一版只做只读、不追问、必填令牌上限**。
5. **启动确认**：推荐**照用户 MCP 服务的做法，按指纹确认一次**。
6. **会话**：推荐**每个定时任务一个固定会话**，历史连续。另一个做法是每次运行一个新会话，互不影响但网页上会话很多。
7. **错过的时间点**：推荐**只补跑 12 小时内最近的一次**。
8. **实施**：推荐**分两次交给 Gemini**：先甲、乙，核对后再做丙；只做离线测试；记录新开 **D-101**（甲、乙）和 **D-102**（丙）。

## 9. 实施记录

### 甲、乙（网页任务计量、阻塞任务过期，2026-11-25）

- **甲（网页任务计量）**：
  - `app/config.py` 新增 `web_run_max_tokens`（默认 `None`，`ge=1`）和 `web_run_max_seconds`（默认 `None`，`gt=0`）。
  - `app/api/runs.py` 的 `RunManager.__init__` 新增 `max_run_tokens`、`max_run_seconds`，由 `app/api/app.py` 的 `create_app` 透传、`app/api/__main__.py` 从配置传入；`RunManager._execute` 在调用 `self._service.query_result(...)` 外面套 `with metering(RunMeter(max_total_tokens=..., timeout_seconds=...)):`，选模式调用与对话令牌上限均计入同一次运行的计量器，用量结果仍由 `LoopRun` 产出。
- **乙（阻塞任务过期）**：
  - `app/config.py` 新增 `blocked_task_ttl_seconds`（默认 `86400.0`，范围 `(0, 30 * 86400]`）。
  - `app/services/rag_agent_service.py` 的 `RagAgentService.__init__` 新增 `blocked_task_ttl_seconds` 和 `clock`（默认 `time.monotonic`），增加 `_blocked_at` 字典与 `_expire_blocked()`：任务进入阻塞时记录时间，`_open_task` 确认续跑后立即从 `_blocked_at` 移除以避免续跑中被清理，续跑后再次阻塞重新计时；在 `query_result`、`_live_blocked`、`resumable` 中调用 `_expire_blocked()`，过期调用 `_forget_task`，不改写归档。
- **限额显示与模板**：`app/agent/run_limits.py` 增加阻塞任务保留时间（整小时按小时显示，否则按秒）及网页运行令牌上限、时限三行；`.env.example` 增加三项注释说明。
- **离线测试**：新增 `tests/test_web_metering.py` 和 `tests/test_blocked_task_expiry.py`，均为假模型与假时钟，未调用真实模型或服务。
- Claude 核对甲、乙（2026-11-25）：逐个看过计量器、设置、过期清理和续跑保护的改动，没有要改的地方；自己重跑 `init.ps1`：2394 passed、28 skipped（基线 2382，新增 12 个），`tests/fixtures/` 没有变化，新文件都是 LF；用户没提交的文件没有被碰过；`data/` 下没有新文件。记为 D-101。

### 丙（定时任务，2026-11-25）

- **配置与确认**：`app/config.py` 新增 `schedules_file`（默认 `schedules.toml`）与 `schedules()`；新增 `app/api/schedules.py`，实现 `Schedule`、`ScheduleError`、`load_schedules`/`parse_schedules`、`next_run`、`latest_due`、`confirm_schedules`（指纹落盘 `data/schedules_confirmed.json`）、`format_schedule_table` 与 `Scheduler` 调度循环。
- **运行与立即拒绝**：`app/api/approvals.py` 新增 `DenyAsker`；`app/api/runs.py` 的 `Run` 增加可选 `scheduled` 字段，`RunManager` 新增 `start_scheduled(schedule, planned_at)` 复用 `_start`/`_execute` 的去重、会话忙、并发上限与收尾逻辑，固定单一循环、只读、关闭长期记忆与 `allow_ask_user=False`、使用任务预算计量器与 `DenyAsker` 的 `RuleGate`，并在 `accepted` 事件附加 `schedule: {name, planned_at}`（不含目标原文）；普通网页运行路径不变。
- **后端与前端接入**：`app/api/__main__.py` 增加 `--confirm-schedules`，启动时校验配置、要求开启 `CONVERSATION_STORAGE_DIR`、按 `WorkspacePolicy` 检查工作目录、执行指纹确认并打印任务表；`app/api/app.py` 在 `lifespan` 启停 `Scheduler`，`GET /api/health` 新增 `schedules`（不含目标原文）；`app/api/static/index.html`、`app.js`、`app.css` 增加顶栏“定时任务 N 个”与 `schedule-` 会话的“定时”标签。
- **模板与忽略**：新增 `schedules.example.toml`；`.env.example` 增加 `SCHEDULES_FILE` 说明；`.gitignore` 增加 `schedules.toml` 与 `data/schedules_confirmed.json`。
- **离线测试**：新增 `tests/test_schedules.py`（假模型、假服务、假时钟），更新 `tests/test_web_api.py` 的健康接口完整字典断言以包含 `schedules: []`；未调用真实模型或服务。
- Claude 核对丙（2026-11-25）：
  - 逐个看过 `app/api/schedules.py`、`RunManager.start_scheduled`、`DenyAsker`、入口、健康接口和页面的改动；普通网页运行走 `else` 分支，和以前一样；
  - 改了一处：定时任务写了 `workspace`、但网页没有开启工作目录选择时，服务会拒绝这个目录，每次到点都会失败。现在启动时就拒绝（退出码 2，说明去掉 `workspace` 就用默认目录），`tests/test_schedules.py` 补了这一种情况；
  - 实施说明比方案多定的一条：有启用的定时任务时必须设置 `CONVERSATION_STORAGE_DIR`，“同一时间点重启后不重跑”靠归档回放；
  - 自己重跑 `init.ps1`：2435 passed、28 skipped（基线 2394，新增 41 个），`tests/fixtures/` 没有变化，新文件都是 LF；用户没提交的文件没有被碰过；`data/` 下没有新文件，仓库根目录没有 `schedules.toml`；
  - 没有调用真实模型。
