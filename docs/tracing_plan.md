# 追踪（tracing）：修改方案

日期：2026-11-09。状态：**已实现**（2026-11-10，trace-001，D-073）。用户认可方案，第 9 节按推荐处理：第一阶段如第 2 节；入口在每条回复下面；评测 `--keep-traces` 一并做；服务商返回的模型名并入（`after_model.response_model`）；Codex 额度有限，由 Claude 实现。与方案不同的地方见第 10 节。

## 1. 背景

用户 2026-11-09 提出“希望加入 tracing”：一次任务跑完以后，能看清它经过了哪些步骤、每次模型调用和工具调用花了多久、用了多少令牌、哪里失败。

现在已经有的基础：

- **事件点**（D-059，`app/agent/hooks.py`）：`task_start`、`task_end`、`plan`、`step`、`before_model`、`after_model`、`before_tool`、`after_tool`，单一循环另有 `todo`。普通模式、单一循环、团队成员、规划、路由和收尾的模型调用都经过 `HookedModel`，工具调用都经过 `HookedTool`。
- **审计记录**（D-060，`AuditHook`）：每次运行结束时，把这些事件写成一个 JSON Lines 文件，放在会话归档目录的 `audit/` 下，每条带相对时间 `t`。只含宿主生成的字段，没有模型原文、工具结果和异常原文。
- **逐次用量**（D-056、D-063）：`after_model` 带这次调用的耗时、输入、输出和缓存命中令牌数。

现在缺的：

1. **事件之间没有关系。**`before_model` 和 `after_model` 没有共同的编号，也不知道属于哪一步、哪个团队成员。团队成员并行时，按先后顺序配对会配错。
2. **没有地方看。**审计文件只能写不能读：归档没有读取方法，服务和网页没有接口，命令行也没有查看命令。
3. **评测不产生记录。**评测关闭了对话归档，所以评测运行没有审计文件。2026-11-09 排查 `fix_split_amount` 时只能靠报告里的令牌数推断。

## 2. 这次做什么

**第一阶段（trace-001，本方案的范围）**：本机追踪，不加依赖，不记原文。

1. 给事件加上编号和父子关系，审计文件升到第 2 版。
2. 新增读取和“把事件拼成一棵树”的代码。
3. 命令行查看命令、网页接口和网页上的“执行过程”面板。
4. 评测可以用 `--keep-traces 目录` 保留每题的追踪文件。

**不在这次范围内**（第 8 节说明，各自需要用户另外决定）：

- 记录提示词、模型回复和工具结果的原文。
- 导出到 OpenTelemetry 或其他外部追踪系统。
- 运行中实时刷新的追踪（运行中的进度仍用现有的进度事件）。

## 3. 名词

- **追踪**：一个任务一次运行的全部事件，拼成一棵树。任务阻塞后恢复是另一次运行，有自己的文件；同一个任务的几次运行按开始时间排在一起看。
- **区间**（span）：树上的一个节点，有开始时间、耗时、状态。种类：`task`（一次运行）、`route`、`plan`、`step`、`member`（团队成员的一段工作）、`model`、`tool`。
- **作用域**：当前正在进行的区间。新开始的模型调用和工具调用，父节点就是当前作用域。

## 4. 逐个文件的修改

### 4.1 `app/agent/hooks.py`

1. **作用域。**新增上下文变量和上下文管理器：

   ```python
   @contextmanager
   def trace_scope(kind: str, **labels):   # kind: 'route' | 'plan' | 'step' | 'member'
       ...
   ```

   - 进入时生成区间编号（进程内递增计数加随机前缀即可，例如 `s-3f9a-12`，不用 UUID 库以外的东西），发出 `{'type': 'scope_start', 'span_id', 'parent_id', 'kind', **labels}`；退出时发出 `{'type': 'scope_end', 'span_id', 'kind', 'seconds', 'error': 异常类型名或 None}`。
   - `labels` 只接受短的宿主值：`index`（步骤序号）、`revision`（计划版本）、`member`（成员角色名，长度上限 64）。不接受模型写的文字。
   - 没有观察者也没有拦截者时直接 `yield`，不生成编号，保持现在的零开销。
   - 上下文变量随 `asyncio` 任务复制，团队成员并行时各自有各自的作用域，这正是需要的行为。
2. **模型调用和工具调用带编号。**`HookedModel.generate` 和 `HookedTool.invoke` 进入时生成 `span_id`，`before_*` 和对应的 `after_*`（包括被拒绝和抛异常的分支）都带上同一个 `span_id` 和当时的 `parent_id`。其余字段不变。
3. **事件类型。**`EVENT_TYPES` 加上 `scope_start`、`scope_end`；`GATE_EVENTS` 不变；`CallbackHook.FORWARDED` 不变，所以进度回调和网页的进度流看不到新事件。
4. `task_start`、`task_end` 带上任务这一层的 `span_id`（`parent_id` 为空），并把它设成运行期间的最外层作用域。这一处写在 `runtime.py`（见 4.2），`hooks.py` 提供一个内部用的 `_root_scope` 帮助函数即可。

**兼容**：新字段都是追加的；现有观察者和拦截者按键取值，不受影响。`tests/` 里逐字比较事件的测试要同步更新期望值（先搜 `before_model`、`after_tool` 的断言）。

### 4.2 设置作用域的位置

| 作用域 | 位置 | 说明 |
| --- | --- | --- |
| `task` | `app/agent/runtime.py` 的 `run()` | 包住 `_run_locked()`，和现有的 `task_start`、`task_end` 在同一处 |
| `plan` | `runtime.py` 调用规划器的地方 | `revision` 为这次规划要产生的版本；单一循环的固定计划不调用模型，可以不设 |
| `step` | `runtime.py` 调用步骤执行器的地方 | `index`、`revision` |
| `route` | `app/services/rag_agent_service.py` 的 `_choose_route` | 已有 `model_purpose('route')`，在同一处加 |
| `member` | `app/agent/team_v2.py` 运行一个成员会话的地方 | `member` 为角色名；负责人自己的调用直接挂在 `step` 下 |

**约束**：`app/agent/planner.py`、`app/agent/step_executor.py`、`app/agent/tool_calling_step_runner.py` 里有用户未提交的中文注释，**这三个文件不要改**。上面的位置都选在调用方（`runtime.py`、`team_v2.py`、服务），不需要动它们。如果实现时发现非改不可，停下来告诉用户。

注意：`route` 发生在运行时的 `run()` 之前，这时还没有 `task` 作用域，也不在运行计量器之内（D-060 的已知情况）。`route` 区间的 `parent_id` 为空，拼树时按时间挂在任务节点之前即可，不要为此调整路由和运行时的先后顺序。

### 4.3 `app/agent/builtin_hooks.py`

- `AUDIT_VERSION` 改为 2。写入逻辑不变（仍在 `task_end` 时一次写完，上限 2000 条）。
- 记录头加 `'trace': True`，表示事件带区间编号。
- 新增 `TraceFileHook(directory)`：和 `AuditHook` 写同样的内容，但写到调用方给的目录，文件名同样由开始时间和任务编号组成。给评测用（见 4.7），因为评测关闭了归档。两者共用一个内部函数生成记录，不要复制一份。

### 4.4 新文件 `app/agent/trace_view.py`

纯函数，不读写文件，不依赖服务：

```python
def build_trace(records: list[dict]) -> dict: ...
def render_text(trace: dict) -> str: ...
```

- `build_trace` 输入一个审计文件的全部记录（第一条是记录头），输出：

  ```python
  {'version': 2, 'started_at': ..., 'task_id': ..., 'status': ..., 'seconds': ...,
   'mode': ..., 'read_only': ..., 'dropped': 0, 'complete': True,
   'totals': {'model_calls': n, 'tool_calls': n, 'prompt_tokens': n, 'completion_tokens': n, 'cached_tokens': n},
   'spans': [{'id', 'parent', 'kind', 'label', 'start', 'seconds', 'status', 'attrs': {...}, 'children': [...]}]}
  ```

- `label` 由宿主字段拼出：模型调用是用途（`plan`、`step`、`wrap_up`、`route`），工具调用是工具名，步骤是序号，成员是角色名。`attrs` 放令牌数、`args_summary`、`denied_by`、`error`（异常类型名）、`chars`、`side_effect` 等原事件里已有的字段，不加工。
- **容错**：
  - 只有 `before_*` 没有 `after_*`（被取消、进程中断、超过 2000 条被丢弃）：区间标为 `status: 'unfinished'`，`seconds` 为空，`complete` 设为 `False`。
  - 父节点找不到：挂到任务节点下。
  - 第 1 版文件（没有 `span_id`）：按出现顺序给模型调用和工具调用配对，结果是一层平铺的列表，`version` 为 1。有并行成员时配对可能不准，这是旧文件的固有限制，在返回值里用 `'paired_by_order': True` 标明。
  - 未知事件类型（包括 `todo`）：`todo` 作为一个零耗时的节点保留条目数，其他忽略。
- `render_text` 输出缩进的文字树，供命令行和排查用，例如：

  ```
  任务 0ccc29ef  completed  16.0 秒  模型 2 次  工具 2 次  输入 4,686 / 输出 499 / 缓存 0
  └─ 步骤 1  15.8 秒
     ├─ 模型 step  3.1 秒  输入 1,901  输出 50  请求工具 2
     ├─ 工具 read_file  0.02 秒  成功  1,204 字符  path=SPEC.md
     ├─ 工具 read_file  0.02 秒  成功  2,310 字符  path=allocate.py
     └─ 模型 step  12.4 秒  输入 2,785  输出 449
  ```

  文字用中文，路径、工具名、用途保持原样。

### 4.5 归档和服务

1. `app/services/conversation_archive.py`：
   - `list_audits() -> list[str]`：`audit/` 下的文件名（不含扩展名），按名字排序；目录不存在时返回空列表。
   - `load_audit(name) -> list[dict]`：校验名字（同 `save_audit` 的正则）和路径（`_check_path`），逐行解析；单行坏了就跳过并计数，文件大小超过上限（建议 8 MB）时拒绝。
   - 只读，不改已有的写入、压缩和删除逻辑。
2. `app/services/rag_agent_service.py`：
   - `async def task_traces(session_id, task_id, *, user_id=None) -> list[dict]`：按现有的身份和会话校验找到归档，取文件名里任务编号对得上的审计文件（一次任务可能有几次运行），逐个 `build_trace`，按开始时间排序返回。没有归档或没有文件时返回空列表。
   - 文件读取放在 `asyncio.to_thread` 里，和 `session_turns` 的做法相同。
   - 不改 `query_result`、hooks 的组装顺序和任何运行路径。

### 4.6 网页

1. `app/api/app.py`：`GET /api/sessions/{session_id}/tasks/{task_id}/trace`，鉴权和编号校验同现有接口；返回 `{'runs': [...]}`；没有记录时返回 404，错误码 `trace_unavailable`，说明“这次任务没有执行记录（可能是未开启归档或审计）”。
2. `app/api/static/`：
   - 每条已完成的回复下面加一个“执行过程”按钮（`_turn_view` 已经带 `task_id`）。点开后请求上面的接口，在回复下方展开面板。
   - 面板内容：顶部一行总计（耗时、模型调用次数、工具调用次数、输入、输出、缓存命中令牌）；下面是按树缩进的列表，每行一个区间，右侧用横条表示它在整次运行中的起止位置（瀑布图）；失败、被拒绝、未结束的区间用不同颜色并写明原因字段。
   - 一个任务有多次运行（阻塞后恢复）时，按顺序分段显示，标“第 1 次运行”“第 2 次运行”。
   - 所有内容只作为文本节点放进页面（`app.js` 第 3 行的约定）；页面文字用中文。
   - 记录头的 `dropped` 大于 0 或 `complete` 为假时，在面板顶部提示“记录不完整”。
3. 不改进度事件流（`/api/runs/{run_id}/events`）和它的白名单。

### 4.7 命令行和评测

1. 新增 `python -m app.agent.trace`：
   - `python -m app.agent.trace 文件.jsonl`：直接读一个审计文件，打印文字树。
   - `python -m app.agent.trace --session 会话名 [--user-id 用户] [--task 任务编号]`：从归档里找；不给任务编号时列出这个会话有哪些运行（开始时间、任务编号、状态、耗时）。
   - `--json` 输出 `build_trace` 的结果。
   - 退出码：0 正常，1 没找到，2 参数错误。只读，不连接模型、MCP 或数据库。
2. `bench/run_bench.py` 加 `--keep-traces 目录`：把目录通过环境变量 `ALETHEIA_TRACE_DIR` 传给子进程；`app/agent/__main__.py` 看到这个变量（且目录在智能体的读写根目录之外）时，给服务加一个 `TraceFileHook`。文件名带上任务、模式和序号，方便对上报告里的行。和 `--keep-answers` 一样，参数后面必须跟目录；`common.md` 里写清楚。
3. 评测报告本身的字段不变。

## 5. 测试（都是确定性测试，不调用真实模型）

新增 `tests/test_trace.py`，或分到现有的 hooks、网页测试文件里：

1. **事件编号**
   - 一次模型调用的 `before_model`、`after_model` 带相同的 `span_id`；成功、抛异常、被拦截者拒绝三种情况都成立。工具调用同理。
   - 在 `trace_scope('step', index=1)` 里发生的调用，`parent_id` 等于这个作用域的编号；嵌套作用域的父子关系正确。
   - 两个并行的 `asyncio` 任务各自进入 `member` 作用域，各自的调用挂在各自的作用域下，不串。
   - 没有任何 hook 时，`trace_scope` 不发事件，`HookedModel`、`HookedTool` 仍然直接透传。
   - `labels` 里传入不允许的键或过长的值时被拒绝或截断（二选一，写进决策记录）。
2. **运行时**：用假模型跑一个两步的规划模式任务和一个单一循环任务，审计文件里有 `task`、`step` 作用域，模型和工具调用都挂在对应步骤下；`CallbackHook` 收到的事件和改动前逐字相同。
3. **团队**：用现有团队测试的假模型，成员的调用挂在 `member` 作用域下，负责人的调用挂在 `step` 下。
4. **`build_trace`**
   - 正常文件：树的形状、每个区间的耗时、总计和 `task_end` 里的数字一致。
   - 缺 `after_*`、父节点缺失、`dropped` 大于 0、空文件、只有记录头。
   - 第 1 版文件：平铺、`paired_by_order` 为真、不抛异常。可以用现在 `tests/` 里审计测试生成的记录做样本。
   - 输出里没有任何输入之外的字段来源（不读文件、不调用服务）。
5. **`render_text`**：对一个固定的小例子逐字比较输出。
6. **归档**：`list_audits`、`load_audit` 的排序、非法名字、目录外路径、坏行、超大文件；别的会话和别的用户的文件读不到。
7. **服务和网页接口**：有归档时返回运行列表；阻塞后恢复的任务返回两次运行；没有记录返回 404 `trace_unavailable`；没有令牌返回 401；非法编号返回 400。
8. **命令行**：解析器、三种调用方式、退出码。
9. **评测**：`--keep-traces` 把环境变量传给子进程（沿用 `test_agent_loop.py` 里替换 `launch_agent` 的写法）；`TraceFileHook` 写出的文件能被 `build_trace` 读回。
10. **前端**：没有自动测试，用 `node --check app/api/static/app.js` 检查语法；面板用浏览器加假模型人工看一遍，结果写进验收记录。

完成标准：`init.ps1` 通过，测试数在 1721 passed、16 skipped 的基础上增加；用假模型在浏览器里看过面板。

## 6. 记录（按 AGENTS.md 的项目规矩）

- `feature_list.json`：实现期间把 `trace-001` 设为 `in_progress`、`active_feature` 设为它，完成后改回。
- `docs/DECISIONS.md`：新增 D-073，写明区间编号的生成方式、作用域放在哪几处、第 1 版文件怎么兼容、为什么不记原文、为什么不接外部系统。
- `progress.md`、`session-handoff.md`：各加一条。
- `common.md`：加查看命令和 `--keep-traces` 的用法。
- `docs/hooks_design.md` 不改（历史设计稿）；`docs/PROJECT_HANDOVER.md` 模块表加 `trace_view.py`，更新测试数；`AGENTS.md` 更新测试数。

## 7. 给实现者的约束

- 先读 `AGENTS.md`，按里面的规则做：不改 `.env`、模型默认值、依赖和业务数据；不安装、不下载；不自动提交、推送；不调用真实模型。
- **不新增依赖。**区间编号、树、瀑布图都用标准库和现有前端代码完成。
- **不记录原文。**事件里仍然只有宿主生成的字段：不放提示词、模型回复、工具结果、文件内容和异常原文。`labels` 只放序号、版本和角色名。
- 不改 `planner.py`、`step_executor.py`、`tool_calling_step_runner.py`（见 4.2）。
- 不改变任何运行路径的行为：追踪只观察。观察者出错不影响任务（现有保证，不要破坏）；`trace_scope` 自己不能吞掉或改写里面抛出的异常，取消要照常传播。
- 不放宽网页进度事件的白名单，不改现有接口的返回格式。
- 审计文件的写入时机不变（运行结束时一次写完）。
- 用户的 `README.md` 和三个脚本删除项保持原样。

## 8. 以后可能做的（这次不做，各自要用户另外决定）

1. **调试时记录原文**：开关默认关闭（例如 `TRACE_CAPTURE_CONTENT=false`），打开后把每次模型调用的消息、回复和工具结果另存一份，和审计文件分开，随会话删除。这会放宽 AGENTS.md 里“不记录完整私有对话、provider 原文”的约束，还要处理敏感文件内容（D-029）和文件大小，所以单独立项。它正是排查“模型把上下文原样当答案”这类问题最直接的手段。
2. **记录服务商返回的模型名**：在 `after_model` 里加一个 `response_model` 字段（只是一个短字符串，不是原文）。能确认中转站有没有换上游。用户 2026-11-09 说中转站问题暂时不解决，所以没放进第一阶段；改动很小，想要的话可以并进来。
3. **导出到 OpenTelemetry**：区间的结构已经和它对得上，加一个把 `build_trace` 的结果转成 OTLP 的导出器即可，但要新增依赖，数据也可能发到本机以外。
4. **运行中实时追踪**：现在审计在运行结束时才写。要实时看，需要把事件边跑边推给页面，并处理 2000 条上限和并发。

## 9. 需要用户确认的问题

1. **第一阶段的范围**是否就按第 2 节：本机、不加依赖、不记原文？
2. **网页入口**放在每条回复下面的“执行过程”按钮，可以吗？还是想要一个单独的页面列出所有运行？
3. **评测的 `--keep-traces`** 要不要放进第一阶段？（建议要，改动小，下次排查评测失败就能直接看。）
4. **服务商返回的模型名**（第 8 节第 2 项）要不要顺手并进来？
5. **由谁实现**：按约定 Codex 有额度就交给它，Claude 审查；否则 Claude 自己做。

## 10. 实现与方案的差别（2026-11-10）

- 网页按钮叫“执行记录”，不叫“执行过程”：运行中的进度区域已经叫“执行过程”，两者分开免得混淆。
- `load_audit` 返回 `(记录, 跳过的坏行数)`；服务把坏行数放进 `skipped_lines`，有坏行时 `complete` 为假。
- 作用域标签只收 `index`、`revision`、`member`：未知键和类型不对直接抛错（属于程序错误），成员名超过 64 字符截断。
- 服务商模型名存在计量器单独的列表里（`RunMeter.response_models`），不进用量快照和评测报告；只收不超过 100 个可打印 ASCII 字符的值。
- 单一循环的 `todo` 事件带上 `parent_id`；追踪里只保留条目数和完成数，不保留模型写的条目文字。
- 命令行多了 `--web`（用网页的用户编号）和 `--memory-dir`。评测额外传 `ALETHEIA_TRACE_NAME`（题目-模式-序号）作文件名前缀；追踪目录落在智能体读写根目录之内时，子进程不写文件。
