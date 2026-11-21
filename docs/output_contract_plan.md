# 结构化输出契约（方案）

日期：2026-11-20。状态：**已实施，未用真实模型验证**（D-093）。第 8 节四条按推荐定（用户 2026-11-20）。由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 核对（用户 2026-11-20 定）。

来源：用户 2026-11-20 定的三阶段收尾计划第三阶段 3.1（记在 `handoff` 分支的 `progress.md`），想法出自用户自己的 `docs/agent_feature_ideas.md` 第 5.1 节。那一节写在 D-085～D-092 之前，下面的现状都按当前代码（`73b235d`）重新核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有用户自己没提交的改动。

## 1. 要解决什么

用户或调用方程序给出一个 JSON Schema，比如“从这份合同里抽出甲方、金额、到期日”。宿主检查最终答案：

- 能解析、符合 Schema，才交付；
- 不符合，就把具体错误告诉模型，给一次修改机会；
- 改了还不符合，任务按失败结束，不交付格式错的结果。

批处理、定时任务、对外 MCP 服务（想法清单 4.1～4.3）的接收方都是程序，都要靠这一条。

## 2. 现状（已核实）

- **不变量**（`AGENTS.md`）：“宿主拥有最终输出契约和验收门”；“格式修复不得重放完成的工具”。
- **单一循环交答案的地方**（`app/agent/agent_loop.py` 的 `AgentLoopRunner.run`）：模型回纯文本时依次检查以下三项，都通过后才 `return self._output(text, ...)`。
  1. 空回答，给 `_REPAIRS` 次修复机会；
  2. 完成门控 `_before_finish`，拒绝一次后再给一次机会，第二次就失败；
  3. D-087 的完成提醒 `REMIND_TO_RUN`，只提醒一次。
  
  这几次重新要答案，都只是在消息末尾追加一句宿主说明，不会重新执行工具。
- **门控拿不到答案原文**：`_before_finish` 发出的事件只有 `writes`、`passing_run_after_last_write`、`review_after_last_write` 三个字段，注释写明“防止调用方意外记录模型原文”（D-059）。所以契约检查不能做成门控，只能放在执行器里。
- **严格 JSON 解析已经有了**：`app/agent/json_output.py` 的 `load_json`（拒绝重复字段、非有限数）和 `unwrap_json_fence`（去掉 ```json 围栏），路由选择就在用（`route_selection.py`）。
- **JSON Schema 校验库**：环境里已经装了 `jsonschema` 4.26.0，因为锁定的 `mcp==1.26.0` 依赖 `jsonschema>=4.20.0`；但 `pyproject.toml` 没有直接写它。
- **失败类型**：`app/agent/runtime.py` 的 `FAILURE_MESSAGES` 里已有 `format`（“模型多次没有按要求的格式给出步骤结果”）。`loop_run.py` 把 `StepFormatError` 映射成 `format`。
- **约束怎么到模型那里**：`RunState.add_constraint` 记在任务里，随任务上下文发给模型，续跑时保留。人设和个人命令就是这样加指令的（`rag_agent_service.py` 的 `_prepare_task`）。
- **团队模式**：负责人也是 `AgentLoopRunner`。但团队有“自动完成”：成员写了文件、通过了检查，宿主就直接拿 `HostToolResult.answer` 当答案（`agent_loop.py` 里 `reply.answer` 那一段）。那份答案模型没机会改。
- **入口**：
  - 服务：`RagAgentService.query_result`，每个请求可选 `execution_mode`、`allow_writes`、`persona`、`command` 等；
  - 命令行：`app/agent/__main__.py`，有 `--result-json`；
  - 网页接口：`app/api/app.py` 的 `MessageIn` 和 `RunOptionsIn`。

## 3. 设计

### 3.1 契约对象（新文件 `app/agent/output_contract.py`）

- `OutputContract(schema)`：
  - 构造时用 `jsonschema` 的 Draft 2020-12 校验器检查 Schema 本身是否合法；不合法就抛 `ValueError`，任务不开始。
  - Schema 序列化后最多 8,000 字符，太大也拒绝，因为它要整份发给模型。
- `check(text)`：
  - 先 `unwrap_json_fence`，再 `load_json`，最后按 Schema 校验；
  - 返回“通过（解析出的文档）”或“不通过（最多 5 条错误，每条带出错位置和说明）”。
- 不支持远程 `$ref`：`jsonschema` 默认就不会去网上取，引用解析不了就按 Schema 不合法处理。`format` 只作说明，不强制检查（`jsonschema` 默认就是这样）。

### 3.2 告诉模型要什么格式

- 新任务开始时加一条约束：宿主写的英文说明（只回一个符合下面 Schema 的 JSON 文档，不加其他文字），后面附 Schema 原文。
- 走约束的好处：
  - 系统提示不变，黄金对照文件不受影响；
  - 续跑时保留；
  - 和人设、个人命令走同一条路。

### 3.3 检查和修复（在 `AgentLoopRunner` 里）

- `AgentLoopRunner` 新增参数 `output_contract=None`。不给时行为一字不变。
- 交答案的顺序：空回答 → 完成门控 → 完成提醒 → **契约检查**。放在最后，是为了不让别的检查要求改答案之后，又绕过契约。
- 第一次不合格：
  - 把模型的回答追加进消息，再追加一句宿主说明：不合格的原因（最多 5 条错误）、只回 JSON、不要调用工具；
  - 这个过程不执行任何工具，不违反“格式修复不重放工具”。
  - 工具预算用完时照样能修，因为修复不需要工具。
- 第二次还不合格：抛 `StepContractError`（`StepFormatError` 的子类）。`loop_run.py` 把它映射成新的失败类型 `contract`，用户看到的是“答案不符合要求的输出格式（Schema），任务停止”。
- 通过时：交付的是模型原来的 JSON 文本（去掉围栏），解析出的文档另外放进结果（见 3.5）。

### 3.4 模式

- 带契约的请求只走单一循环。
  - 请求是 `auto` 的，直接选 single，不调用路由模型；
  - 明确要 `team` 的，拒绝（`ValueError`）。
  - 理由是团队有自动完成，答案是宿主拼的，模型改不了。团队支持以后再说（第 7 节）。
- 团队成员和调研成员不受影响，契约只管交给用户的最终答案。
- `ask_user` 阻塞不是最终答案，不检查；续跑以后的答案照常检查。

### 3.5 入口

- 服务：`query_result(..., output_schema=None)`，接受一个 dict。续跑的任务沿用原任务的契约，不接受新的。
- 命令行：`--output-schema 文件`（UTF-8 的 JSON 文件）。只用于单次目标，交互模式不支持。`--result-json` 里多一个 `output` 字段，放解析出的文档。
- 网页接口：`RunOptionsIn` 加 `output_schema`（对象）。网页界面先不加输入框（第 8 节第 3 问）。

### 3.6 记录

- 新的观察者事件 `output_contract`，只有 `result`（`pass` / `repair` / `fail`）和 `errors`（错误条数），不带答案、Schema 和错误原文（D-059）。
- 用量里加 `usage.contract`：`{'repairs': n}`。轨迹查看器显示一行“输出格式不合格，要求修改一次”。
- 评测行带 `contract`（见第 5 节）。

## 4. 测试（离线，假模型）

- 合格直接交付；带 ```json 围栏的也算合格，交付时去掉围栏。
- 不合格修一次后合格：修复说明里有错误位置；修复期间没有执行任何工具；这次请求的旧消息逐字节不变。
- 两次都不合格：失败类型 `contract`，用户看到中文说明。
- 不是 JSON、重复字段、`NaN`：按不合格处理。
- Schema 不合法、太大、有远程 `$ref`：请求阶段就拒绝，不调用模型。
- 工具预算用完以后也能修。
- 和完成提醒同时出现时顺序正确：先提醒，提醒后的答案再检查契约。
- 模式：`auto` 加契约不调用路由；`team` 加契约被拒绝；续跑沿用原契约。
- 观察者事件和轨迹只有结果和条数，测试里放一段“秘密”文字，断言它不出现在事件里。
- 命令行：`--output-schema` 读文件，`--result-json` 带 `output`。网页接口：字段校验。
- 不给契约时：`init.ps1` 通过，黄金对照文件不变。

## 5. 评测（可选，要用户逐项同意）

- 评测题可以在 `task.json` 里加 `output_schema`，评测脚本带上它运行。
- 现有的 `answer_json` 类问答题（例如 `billing_qa`）各加一份 Schema，有契约和没契约各跑几次，比较格式错误率和通过率。这会调用真实模型，要单独同意。

## 6. 不做的

- 不做团队模式的契约（3.4 节）。
- 不做网页界面上的输入框（除非第 8 节第 3 问选做）。
- 不强制检查 `format`（日期、邮箱等）。
- 不改系统提示，不改工具。
- 不加 JSON 以外的格式（比如 CSV、YAML）。

## 7. 以后可以接着做

- 团队模式：自动完成时按契约检查宿主拼的答案，不合格就退回负责人改一次。
- 批处理（想法清单 4.2）：每行带一份 Schema，结果直接写成 JSON 行。

## 8. 要用户定的

1. **校验怎么做**：
   - 推荐**用 `jsonschema`，并在 `pyproject.toml` 里写明 `jsonschema==4.26.0`**。环境里已经有这个版本，是 `mcp` 带进来的，不用下载；写明以后，就算 `mcp` 以后不再依赖它也不会丢。
   - 另一个做法是自己写一个小子集（类型、必填、枚举、数组元素），不碰依赖，但只能认一部分 Schema。
2. **团队模式**：推荐**这次只支持单一循环**，`auto` 直接走单一循环，明确要团队的就拒绝。
3. **网页界面**：推荐**这次只加接口字段，不加输入框**。
4. **记录**：推荐新开 **D-093**。

## 9. 实施记录（2026-11-20）

- Gemini 按 Claude 写的实施说明一次做完，改了 16 个文件，新增 `app/agent/output_contract.py` 和 `tests/test_output_contract.py`。说明里比方案多定的几点：
  - 契约跟着任务存在 `RunState.output_schema`；
  - 修复那一轮模型仍然调用工具，一律不执行，直接按第二次不合格处理；
  - 修复说明列出错误位置（JSON 指针）和原因，最多 5 条，每条最多 200 字符。
- 写入怎么处理：`auto` 加契约时，宿主直接选 single，“是否只读”沿用请求或服务的写入设置，因为原来由路由给的写入判断这次没有了。
- 方案以外多了一处：网页接口的 `completed` 事件在带契约时多一个 `output` 字段，不带契约时一字不变。
- Claude 核对时改了两处：
  - 修复那一轮服务商出错或超时，原来会被记成 `contract`，改成保持原来的失败类型，只有不合法的工具调用参数才算不合格（补了 2 个测试）；
  - `app/agent/step_models.py` 被写成了 CRLF，改回 LF。
- 检查：`init.ps1` 2192 passed、27 skipped（基线 2169，新增 23 个）；黄金对照文件没有变化；用户没提交的文件没有被碰过（按校验值核对）。没有调用真实模型。
