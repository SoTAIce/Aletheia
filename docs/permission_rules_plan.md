# 按规则的权限控制（方案）

日期：2026-11-22。状态：**已实施，未用真实模型验证**（D-094）。第 8 节五条按推荐定（用户 2026-11-22）。由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写实施说明并核对（用户 2026-11-20 定）。

来源：三阶段收尾计划第三阶段 3.2（记在 `handoff` 分支的 `progress.md`），想法出自用户自己的 `docs/agent_feature_ideas.md` 第 7.2 节。那一节写在 D-085～D-093 之前，下面的现状都按当前代码（`7efead0`）重新核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有用户自己没提交的改动。

## 1. 要解决什么

用户在一个文件里写几条规则，例如：

- “`run_tests` 总是允许”；
- “写 `docs/` 下的文件要问我”；
- “任何工具都不许碰 `secrets/`”；
- “联网搜索每个任务最多 5 次”。

宿主在每次调用工具之前按规则判断：允许、询问还是拒绝。

- 现在命令行完全没有确认，开了 `--write-root` 就可以随便写；
- 网页只能按 `WEB_APPROVAL_TOOLS` 列出的工具一律确认，不能按路径区分；
- 以后接进来的第三方工具（3.3）也需要一个统一的管控入口。

## 2. 现状（已核实）

- **门控**（`app/agent/hooks.py`）：
  - `HookSet.gate` 依次问每个门控，第一个拒绝的说了算；出错、超时、没给决定都按拒绝（第 116～132 行）。
  - 门控只能拒绝，不能改参数（D-058）。
  - 门控可以自己要更长的时限（`timeout_seconds`，第 134～137 行），网页确认就是这样等人的。
- **`before_tool` 门控事件**（`HookedTool.invoke`，第 408～430 行）：
  - 带 `tool`、`effect`（D-087）、`side_effect`、`args_summary`；
  - 还带只给门控看的 `args_preview`（D-080）。内置文件和沙箱工具的 `path` 是原样的字符串，最长 2,000 字符，超长时变成带 `omitted_chars` 的字典。
  - 被拒绝的调用不执行，模型收到 `not executed: <原因>`；`after_tool` 事件带 `denied_by`（门控名）。
  - 第 423 行注释：先过门控再调用，依赖工具逐个执行。
- **网页确认**（`app/api/approvals.py` 的 `ApprovalGate`，D-061）：
  - 对 `WEB_APPROVAL_TOOLS`（默认 `write_file,edit_file,run_python,run_tests`）和属性是 `external` 的工具问人；
  - 同一时间只问一个，`allow_all` 对本次运行其余的调用有效；
  - 等待时用 `extend_deadlines` 把时限往后推，超时（`WEB_APPROVAL_TIMEOUT_SECONDS`，默认 300 秒）按拒绝。
  - 由 `app/api/runs.py` 第 205～209 行每次运行加进 `hooks`。
- **命令行**（`app/agent/__main__.py`）：
  - 只有追踪钩子（`trace_hooks`），没有任何确认；
  - 交互模式用 `asyncio.to_thread(input, ...)` 读一行（第 307～308 行）；
  - 启动时系统盘目录的确认（`confirm_startup`，`app/services/workspace.py` 第 172 行）用 `sys.stdin.isatty()` 判断能不能问人，不能问就拒绝。
- **服务组装钩子**（`RagAgentService._run_hooks`，第 334～354 行）：服务的钩子、请求自带的钩子、审计、对话令牌上限，每次运行组装一次。
- **工具内部的底线**：
  - D-029：密钥路径不可读，自动执行位置不可写。
  - D-074：`WorkspacePolicy.closed_folders()`（`WORKSPACE_DENY_DIRS` 加项目 `data`、对话归档、人设目录）通过 `--deny` 传给文件服务和沙箱服务。工作目录里的这些目录，文件工具当它们不存在（读、列、搜、写都拒绝），沙箱用空目录盖住。
  - 这份清单只有绝对路径。每次运行按选中的工作目录启动服务时，`tool_loader.workspace_servers` 的闭包 `servers(root, writable)` 带上同一份清单。
- **配置文件的先例**：`models.toml`（D-084）：
  - 设置 `MODEL_PROFILES_FILE`，默认 `models.toml`；默认文件不存在就当没有；
  - 明确指定了却找不到，或者文件有错，就抛 `ModelProfileError`，拒绝启动；
  - 用 `tomllib` 读，只认识列出的键。
- **评测**（`bench/run_bench.py` 第 262～266 行）：给子进程清掉 `PERSONAS_DIR`，理由是“用户自己的人设不能改变评测”。

## 3. 设计

### 3.1 规则文件 `permissions.toml`

- 新设置 `PERMISSIONS_FILE`，默认 `permissions.toml`，读法照 `models.toml`：
  - 默认文件不存在：没有规则，行为和现在一字不差；
  - 明确指定了却找不到、TOML 有错、有不认识的键或取值：抛 `PermissionRulesError`，命令行和网页都拒绝启动，错误说明是中文，不带文件内容。
- 新模板 `permissions.example.toml`，`.gitignore` 加 `permissions.toml`，和 `models.toml` 一样。
- 格式：

  ```toml
  [[rule]]
  tool = "run_tests"
  action = "allow"

  [[rule]]
  tool = ["write_file", "edit_file"]
  path = "docs/"
  action = "ask"

  [[rule]]
  path = "secrets/"
  action = "deny"

  [[rule]]
  tool = "search_web"
  action = "allow"
  max_calls = 5
  ```

- 每条规则的字段：

  | 字段 | 取值 | 不写时 |
  |---|---|---|
  | `action` | `allow`、`ask`、`deny`，必填 | — |
  | `tool` | 工具名或工具名列表 | 所有工具 |
  | `effect` | `read`、`write`、`run`、`external` 之一或列表（D-087） | 不限 |
  | `path` | 相对工作目录的路径前缀，用 `/` 分隔 | 不看路径 |
  | `max_calls` | 每个任务最多几次，正整数 | 不限 |

- 预检：
  - `path` 不能是绝对路径，不能含 `..`，不能是空或 `.`（整个工作目录用别的办法关）；
  - `max_calls` 不能和 `deny` 一起用；
  - 最多 200 条。

### 3.2 怎么判断（新文件 `app/agent/permission_rules.py`）

- `PermissionRules`：解析好的规则，启动时读一次，之后不变。
- `RuleGate`：`before_tool` 门控，每次运行新建一个，带上：
  - 这次任务的工作目录（用来把绝对路径换成相对路径）；
  - 一个“问人”的对象（见 3.3），可以没有。
- 一条规则“命中”：工具名、属性都对得上，并且：
  - 规则没写 `path`；
  - 或者这次调用的路径参数规范化后，落在规则的路径前缀之下（按路径段比较，`docs` 不会命中 `docsx/`）。
- 路径规范化（只做字面处理，不碰文件系统）：
  - `\` 换成 `/`；去掉 `.` 段，合并 `..`；
  - Windows 上比较前统一小写；
  - 绝对路径落在工作目录之下的，换成相对路径。
  - 换不了：绝对路径在工作目录之外、`..` 越出工作目录、路径被截短、不是字符串。这时这次调用的路径算“不明”，所有带路径的 `ask` 和 `deny` 规则都算命中，宁可多问、多拒。
  - 没有路径参数的调用（比如 `search_web`），带 `path` 的规则不命中。
- 决定：
  - 命中的规则里，`deny` 优先，其次 `ask`，最后 `allow`；
  - 一条规则也没命中时，用原来的默认：命令行允许；网页按 `WEB_APPROVAL_TOOLS` 和 `external` 属性问人（第 8 节第 2 问）；
  - 命中的规则有 `max_calls`、而且这个任务已经用够了，就拒绝，原因写明“本任务已调用 N 次，规则上限 N”。只有真正放行的调用才计数，团队成员共用同一个门控，所以是整个任务合计。
- 拒绝时给模型的原因是宿主写的英文：`denied by permission rule 3`，加上是哪类原因（拒绝、上限、无人确认、用户拒绝）。不写参数原文。

### 3.3 “询问”怎么问

- **网页**：
  - 把 `ApprovalGate` 拆成“怎么问”和“问谁”两部分，`RuleGate` 用它来问，确认卡片和接口都不变。
  - `approval_required` 事件多一个 `rule`（规则序号，没有规则时不带）。
  - 有规则文件时，网页每次运行只装 `RuleGate`，原来的工具清单变成它的“没命中时的默认”；没有规则文件时，照旧装 `ApprovalGate`，一字不变。
- **命令行**：新的终端确认。
  - 打印工具名、路径和参数摘要（写文件只打印字数和前几行，不打印全文），输入 `y` 允许、`a` 本任务都允许、其他任何输入拒绝；
  - 同一时间只问一个，团队成员排队；
  - 等待时用 `extend_deadlines` 暂停时限，最长等待时间和网页一样，用 `WEB_APPROVAL_TIMEOUT_SECONDS`（第 8 节第 4 问）。
  - 终端输入统一由一个读取器管：交互模式读下一个目标和确认读答案用同一个读取线程。确认超时以后，用户迟到的那一行丢掉，并说明一句，不会被当成新任务。
- **没人可问**：
  - `sys.stdin.isatty()` 为假（评测、管道、以后的定时任务和批处理）时，`ask` 一律按拒绝，原因写“无人确认”；
  - 不等待，不阻塞。

### 3.4 “不许碰”同时交给工具（第 8 节第 3 问）

- 门控只看这次调用自己的路径参数。在工作目录根上 `search_files`、`list_directory`，或者在沙箱里运行代码，都会走进 `secrets/`，门控看不出来。
- 所以：`deny` 规则如果没写 `tool`、`effect`、`max_calls`，只写了 `path`，就按工作目录换成绝对路径，加进这次运行的 `--deny` 清单。启动时的服务和按工作目录启动的服务都加。
- 这样它和 `WORKSPACE_DENY_DIRS` 一样是工具内部的底线：读、列、搜、写、沙箱都碰不到。门控上仍然照常检查，作为第二层。
- 只限定某些工具的 `deny` 规则、所有 `ask` 和 `allow` 规则，只在门控上生效；文件里和 `--show-permissions` 都会写明这一点。

### 3.5 入口和查看

- 服务：`RagAgentService(permission_rules=..., asker=...)`，命令行和网页启动时读规则文件传进去；`_run_hooks` 每次运行新建 `RuleGate`。
- 命令行：新 `python -m app.agent --show-permissions`，打印规则表：每条的序号、动作、工具、路径、上限，以及是否交给了工具。不调用模型，规则文件有错时打印中文错误，退出码 2。
- 网页：健康检查多一个 `permission_rules`（条数）。网页界面不加规则编辑。
- 评测：照 `PERSONAS_DIR` 的先例，给子进程设 `PERMISSIONS_FILE=''`，用户自己的规则不改变评测。

### 3.6 记录

- `after_tool` 事件的 `denied_by` 是门控名 `permissions`，另加 `rule`（规则序号）。不带路径和参数原文（D-059）。
- 用量：`usage.permissions = {'denied': n, 'asked': n}`，只在有规则时出现。
- 轨迹查看器的工具行显示“被权限规则 3 拒绝”。

## 4. 测试（离线）

- **解析**：
  - 每个字段的合法和不合法取值；
  - 不认识的键；`path` 是绝对路径、含 `..`、是 `.`；`max_calls` 和 `deny` 同用；超过 200 条；
  - 默认文件不存在时没有规则；明确指定的文件找不到时报错。
- **匹配**：
  - 工具名、列表、属性；路径前缀按段比较（`docs` 不命中 `docsx/`）；
  - `\` 和 `/`、`./docs/../docs/a.md`、Windows 大小写；
  - 工作目录内的绝对路径；工作目录外的绝对路径、越出的 `..`、截短的路径都算“不明”，带路径的 `deny` 和 `ask` 命中。
- **决定**：
  - 多条命中时 `deny` 优先，其次 `ask`；
  - 没有命中时，命令行允许，网页按原来的清单问人；
  - `max_calls` 用够后拒绝，被拒绝的调用不计数；团队成员合计。
- **询问**：
  - 终端 `y`、`a`、其他输入；超时按拒绝，迟到的那一行被丢掉；
  - 不是终端时 `ask` 直接拒绝，不等待。
  - 网页上 `ask` 规则走确认卡片，`approval_required` 带 `rule`；`allow` 规则让默认清单里的工具不再弹卡片。
- **底线**：只写 `path` 的 `deny` 规则进入 `--deny` 清单，文件服务和按工作目录启动的服务都带上；在根目录上搜索，搜不到 `secrets/` 里的内容。
- **记录**：事件只有门控名和规则序号，测试里放一段“秘密”路径，断言它不出现在观察者事件里。
- **入口**：`--show-permissions` 的输出和错误退出码；评测子进程的环境里 `PERMISSIONS_FILE` 是空。
- **没有规则文件**：`init.ps1` 通过，黄金对照文件不变。

## 5. 不做的

- 不做网页上的规则编辑界面。
- 不做按参数内容（比如命令行参数、搜索词）匹配，只看工具、属性和路径。
- 不改默认行为：没有规则文件时，命令行仍然不确认。
- 不做按会话或按人设的规则，整个服务一份。
- 门控上的路径判断不读文件系统，不解析链接；要真正关住一个目录，用 3.4 节那种规则或 `WORKSPACE_DENY_DIRS`。

## 6. 以后可以接着做

- 3.3（接入用户自己的 MCP 服务器）：第三方工具默认按 `external` 处理，可以直接用 `effect = "external"` 的规则管。
- 以后改成工具并行调用时，要重新检查“先过门控再调用”。
- 按人设覆盖一部分规则（比如某个人设只读）。

## 7. 风险

- **路径绕过**：门控上的判断是字面的，模型可以换写法。所以“不许碰”要交给工具（3.4）；`ask` 规则在路径“不明”时宁可多问。
- **终端输入抢行**：确认和交互模式读的是同一个终端。统一读取器、超时后丢掉迟到的一行，就是为了不把“y”当成新任务。
- **网页默认清单被放宽**：一条 `allow` 规则会让原来要确认的工具不再确认（第 8 节第 2 问）。这是用户自己写的规则，`--show-permissions` 和健康检查都能看到。

## 8. 要用户定的

1. **默认行为**：推荐**没有规则文件时一字不变**：命令行不确认，网页照旧按 `WEB_APPROVAL_TOOLS` 确认。评测和现有用法都不受影响。另一个做法是命令行默认对写入和运行类工具询问，但会让评测和管道用法全部变成拒绝。
2. **`allow` 能不能放宽网页默认清单**：推荐**能**：用户规则命中时就按规则决定，网页的默认清单只在没有规则命中时才用。这样“`run_tests` 总是允许”在网页上也成立。另一个做法是默认清单始终保留，`allow` 只对清单以外的工具起作用。
3. **只写路径的 `deny` 规则同时交给工具**：推荐**交**（3.4 节），不然在根目录上搜索、在沙箱里运行都能碰到被禁止的目录。另一个做法是只在门控上检查，改动更小，但“不许碰”就名不副实。
4. **命令行确认的等待时间**：推荐**沿用 `WEB_APPROVAL_TIMEOUT_SECONDS`**（默认 300 秒），不加新设置。另一个做法是新加 `CLI_APPROVAL_TIMEOUT_SECONDS`。
5. **记录**：推荐新开 **D-094**。

## 9. 实施记录（2026-11-22）

- 第一次启动 Gemini 时，Antigravity 服务端没能新建会话（“empty conversation id”），什么都没改；重试一次就正常了。
- Gemini 按 Claude 写的实施说明一次做完：新增 `app/agent/permission_rules.py`、`app/agent/terminal_input.py`、`permissions.example.toml`、`tests/test_permission_rules.py`，改了 17 个文件。网页确认卡片在有规则时显示“规则 N 要求确认”（`app/api/static/app.js` 一行）。
- 说明里比方案多定的几点：
  - `Decision` 多一个可选的 `rule`，`after_tool` 事件在有规则序号时带上；
  - 网页的确认拆成 `WebAsker`（怎么问）和 `ApprovalGate`（问哪些工具）；没有规则文件时，`ApprovalGate` 的行为不变。
- Claude 核对时改了三处：
  - 网页的规则门控原来按请求里的目录判断，续跑的任务会拿错目录。现在改成由服务告诉门控这次任务核对过的工作目录（`RuleGate.use_workspace`），补了 1 个测试；
  - 命令行确认超时的时候，立刻说明按拒绝处理、下一行会被忽略；
  - `app/api/approvals.py` 被写成了 CRLF，改回 LF。
- 检查：`init.ps1` 2226 passed、27 skipped（基线 2192，新增 34 个）；黄金对照文件没有变化；用户没提交的文件没有被碰过（按校验值核对）。`--show-permissions` 用示例文件和不存在的文件各试过一次。没有调用真实模型。
- 一个限制：任务因为 `ask_user` 暂停、续跑以后，`max_calls` 从零重新计数，和 D-092 的打转计数一样。
