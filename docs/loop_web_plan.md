# 网页可选单一循环：修改方案

日期：2026-11-07。状态：**方案，未实现**（loop-web-plan-001）。打算交给 Codex 实现。

## 1. 背景

- 单一循环（`AgentLoopRunner`，D-062～D-064）现在只能从命令行（`--single-runner loop`）和评测（`bench --single-runner loop`）打开。
- 网页后端 `app/api/__main__.py` 创建 `RagAgentService` 时没传 `single_runner`，所以网页永远走规划模式。
- 真实评测（每题 1 次）中，循环和规划模式通过率相当，调用和令牌约少一半：调参集 11/11 对 11/11，留出集 12/13 对 11/13，写文件 11/11，沙盒 10/11。
- 阻塞恢复已能接着做（D-064）；网页的人工确认（D-061）走的是工具包装层，循环同样经过它，但还没在网页上验证过。

## 2. 这次只做一件事

让网页既能在启动时选默认的执行方式，也能在每条消息上选，用法和现有的"模式 / 写文件 / 长期记忆"三个选项一样（D-051）。

**不在这次范围内**：把默认值从 `plan` 改成 `loop`。这一项要用户另外决定，见第 7 节。

## 3. 名词

- **模式**（`mode`，已有）：`single` 普通，`team` 团队，`auto` 自动。
- **执行方式**（`runner`，新增）：只对普通模式有效。`plan` 是先规划再逐步执行；`loop` 是单一循环。团队模式不受影响。

## 4. 逐个文件的修改

### 4.1 `app/services/rag_agent_service.py`

1. **每个任务都可以单独选执行方式。**
   - 构造参数 `single_runner` 保留，作为默认值。
   - `query_result` 新增关键字参数 `single_runner: str | None = None`：`None` 表示用默认值，否则必须是 `'plan'` 或 `'loop'`，不是就抛 `ValueError`。
   - 校验写在已有的 `execution_mode` 校验旁边（约第 340 行）。
2. **执行方式记进任务选项。**
   - 第 415～418 行组装 `options` 时，加上 `'runner': single_runner or self._single_runner`。
   - 阻塞恢复时，沿用 `self._task_options` 里存的值，忽略本次请求的值，和 `mode` 的做法相同。
3. **判断要不要走循环。**
   - 第 500 行改为 `loop = route == 'single' and options['runner'] == 'loop'`。
   - 构造函数里禁止 `loop` 和自定义 `runner_registry` 同时出现的那项校验，要在运行时也做：如果 `options['runner'] == 'loop'` 且 `self._runner_options['runner_registry'] is not None`，抛 `ValueError`。也可以在 `run_options()` 里不提供 `loop`，两种做法选一种，写进 D-065。
4. **对外说明可选值。**`run_options()`（约第 303 行）返回值加两项：
   - `'runners'`：可选的执行方式。默认是 `['plan', 'loop']`；有自定义 `runner_registry` 时只有 `['plan']`。
   - `'default_runner'`：等于 `self._single_runner`。
5. **`route` 事件。**事件里已经带 `'options': dict(options)`，加上 `runner` 后会自动出现在事件里，不要另加键。`tests/test_progress_and_history.py` 和 `tests/test_run_options.py` 会逐字比较这个事件，要同步改这两处测试的期望值。之前单独加 `runner` 键时，就是这两处测试失败。

### 4.2 `app/api/app.py`

1. `RunOptionsIn` 加 `runner: Literal['plan', 'loop'] | None = None`。
2. `run_options()`（第 160 行）把 `runners` 和 `default_runner` 原样带出去。
3. `resolve()`（第 166 行）：
   - 请求的 `runner` 不在 `runners` 里时，返回 `ApiError(400, 'runner_unavailable', '这台服务不能使用单一循环。')`。
   - 返回值加 `'runner': requested.get('runner') or offered['default_runner']`。
4. `/api/health` 的 `options` 会自动带上这两项，不用另改。

### 4.3 `app/api/runs.py`

- 第 174～175 行把 `run.options` 转成服务参数的地方，加上 `'single_runner': run.options['runner']`。
- 没有 `runner` 键的旧记录（例如测试里手工构造的）按 `None` 处理，即用服务默认值。
- 幂等比较（`requested`，D-051）不用改，因为 `requested` 本来就是原样保存的。

### 4.4 `app/api/__main__.py`

- 解析器加 `--single-runner {plan,loop}`，默认 `plan`，和 `app/agent/__main__.py` 的写法一致。
- 创建 `RagAgentService` 时传 `single_runner=args.single_runner`。
- 启动时打印的那一行加上执行方式，例如：`（写文件：开启，执行方式：单一循环）`。

### 4.5 网页 `app/api/static/`

1. **`index.html`**：在"模式"选择框后面加一个"执行方式"选择框（`id="opt-runner"`），两个选项：
   - `plan`：按计划；
   - `loop`：单一循环。

   提示文字写：`按计划：先列步骤再逐步执行；单一循环：模型边做边决定，调用更少`。
2. **`app.js`**：
   - 在 `OPTIONS_KEY` 的本地保存里加 `runner`，读取时校验它在 `caps.runners` 里，不在就用 `caps.default_runner`（照 `mode` 的写法，第 336～358 行）。
   - 发送消息时，把 `runner` 放进 `options`。
   - 模式是 `team` 时禁用"执行方式"选择框，因为团队模式不看这个值。`auto` 可能落到普通模式，所以保持可选。
   - 回答问题（恢复任务）时，和"模式"一样禁用，提示"回答问题时沿用原任务的设置"（第 369～370 行）。
   - `caps.runners` 只有一项时，隐藏这个选择框。
   - 第 663 行显示路由的地方：普通模式下，在"普通模式"后面加上 `event.options.runner === 'loop' ? '（单一循环）' : '（按计划）'`。
3. **页面上的所有文字都用中文。**模型给出的内容照旧只作为文本节点放进页面（第 3 行的约定）。
4. **可选，不强求**：循环会发出 `{'type': 'todo', 'items': [...]}` 事件（D-062）。如果 `runs.py` 会把它转给页面，可以在进度区显示成清单；如果目前会被过滤掉，就保持过滤，不要为此放宽事件白名单。先确认 `runs.py` 怎么处理未知事件，再决定做不做。

## 5. 测试（都是确定性测试，不调用真实模型）

在 `tests/` 下新增 `tests/test_web_loop_runner.py`，或者补进现有的网页测试里。覆盖以下几点：

1. **服务层**
   - `query_result(..., single_runner='loop')` 走循环：不调用规划模型，`plan` 事件的步骤是 `LoopPlanner.STEP`。
   - 不传这个参数时用构造参数的默认值。
   - 值非法时抛 `ValueError`。
2. **恢复沿用原选择**：以 `loop` 开始的任务阻塞后，恢复请求即使传了 `plan`，也仍走循环，并且接着原来的消息做（复用 `test_agent_loop.py` 里服务测试的写法）。
3. **自定义执行器注册表**：`run_options()['runners'] == ['plan']`，请求 `loop` 时被拒绝。
4. **网页接口**
   - `/api/health` 带出 `runners` 和 `default_runner`。
   - `POST /messages` 带 `options.runner='loop'` 时，服务收到 `single_runner='loop'`。
   - `runner='steps'` 返回 400，错误码 `invalid_input`（由 Pydantic 校验拒绝）。
   - 服务不提供 `loop` 时，返回 400 `runner_unavailable`。
5. **`route` 事件的 `options` 带上 `runner`**：更新 `test_progress_and_history.py` 和 `test_run_options.py` 的期望值。
6. **启动参数**：`app/api/__main__.py` 的解析器接受 `--single-runner loop`，默认是 `plan`。
7. **前端没有自动测试**：前端改动用 `node --check app/api/static/app.js` 检查语法（`init.ps1` 如果已经包含这一步就不用另跑）。

完成标准：`init.ps1` 通过，测试数在 1600 passed、16 skipped 的基础上增加。

## 6. 记录（按 AGENTS.md 的项目规矩）

- `feature_list.json`：
  - 新增 `loop-web-001`，依赖 `loop-web-plan-001`（本方案，已登记为 `done`）；
  - 实现期间把 `active_feature` 设为 `loop-web-001`，完成后改回 `null`。
- `docs/DECISIONS.md`：新增 D-065，写明执行方式按任务选择、恢复时沿用原选择，以及有自定义执行器注册表时选了哪种处理办法。
- `progress.md`、`session-handoff.md`：各加一条记录。
- `AGENTS.md`、`docs/PROJECT_HANDOVER.md`：更新测试数。
- `common.md`：加一条启动命令，例如 `.venv\Scripts\python.exe -m app.api --filesystem-root . --no-knowledge --single-runner loop`。

## 7. 之后的下一步（这次不做）

**把默认值改成 `loop`。**这需要用户决定。动手之前建议先做：

1. 在网页上用 `loop` 真实走一遍，要用户授权：
   - 普通问答；
   - 写文件，并且确认人工确认的"允许"和"拒绝"都正常（D-061）；
   - `ask_user` 阻塞后在网页上回答，确认是接着做的。
2. 调参集和留出集各再跑 1～2 次，确认通过率不比规划模式差。

改的位置有三处：`RagAgentService` 的构造默认值、`app/agent/__main__.py` 和 `app/api/__main__.py` 的 `--single-runner` 默认值、`bench/run_bench.py` 的 `--single-runner` 默认值。

## 8. 给实现者的约束

- 先读 `AGENTS.md`，按里面的规则做：不改 `.env`、模型默认值、依赖和业务数据；不安装、不下载；不自动提交、推送；不调用真实模型。
- 工作区里有用户自己没提交的改动，不要动，也不要一起提交：
  - `README.md`；
  - `app/agent/planner.py`、`app/agent/step_executor.py`、`app/agent/tool_calling_step_runner.py` 里的中文注释；
  - 删掉的 `scripts/benchmark_conversation_memory.py`、`scripts/validate_multi_runner_live.py`、`scripts/validate_summary_context.py`。
- 跑测试时用 `-p no:cacheprovider` 和指向临时目录的 `--basetemp`，不要在项目里留下 `__pycache__` 或 `.pytest_cache`，也不要往 `data/` 写东西。
