# 钩子加固（hooks-fail-closed-001）：修改方案

日期：2026-11-14。状态：**已实现（2026-11-14，D-080），未提交**。
来源：用户 2026-11-14“先写钩子加固方案吧”。待办 `hooks-fail-closed-001` 出自 2026-11-10 的审查（用户问权限检查和钩子之间会不会抢跑）。依据是当前代码（`1cb3841`）。

## 1. 现状

钩子的设计见 [hooks_design.md](hooks_design.md)（D-058～D-061）。和这次有关的事实：

| 项 | 现在 | 位置 |
|---|---|---|
| 没装钩子时 | 上下文变量的默认值是 `EMPTY`（空的 `HookSet`）。`HookedTool.invoke` 和 `HookedModel.generate` 看到没有观察者也没有门控，就直接调用底层工具或模型。“从没装过”和“装了一个空的”分不出来 | `app/agent/hooks.py` 第 142～143、331～333、378～380 行 |
| 谁来装 | 生产路径只有一处：`RagAgentService._execute` 用 `_models(cache_key, run_hooks)` 包住整个运行，`_run_hooks` 组好这次的钩子（服务或调用方的钩子、网页的确认门控、审计、对话令牌上限） | `app/services/rag_agent_service.py` 第 258～298、581 行 |
| 实测 | 用临时 pytest 插件统计全部 1794 项测试：真正“没装钩子就放行”的只有 `HookedModel.generate`（5 项测试）和 `HookedTool.invoke`（2 项）；其余命中都在观察和追踪上（`LoopRun.run`、`trace_scope`、团队事件等），不涉及放行 | 插件只在 scratchpad 里跑过，没进仓库 |
| 网页确认显示什么 | 门控事件里的参数只有 `summarize_arguments` 的摘要：路径、行号、开关这类短值原样保留，模型写的内容只留长度。确认卡片显示“写入文件：a.py，1,234 字符”，用户看不到要写什么 | `hooks.py` 第 300～316 行，`app/api/approvals.py`，`app/api/static/app.js` 第 17～29 行 |
| 要确认的工具 | `write_file`、`run_python`、`run_tests`（`WEB_APPROVAL_TOOLS`）。`run_python_code` 只读挂载工作区，不需要确认（D-028） | `app/config.py` 第 102 行 |
| 工具调用顺序 | 循环对一轮里的多个工具调用逐个执行，没有 `gather`、`TaskGroup` 或 `create_task`。所以“先过门控、再调用”之间不会插进别的调用；`ApprovalGate` 也加了锁并在锁内复查 | `app/agent/agent_loop.py` 第 328、375、438 行 |
| 后台摘要 | 对话摘要用单独的 `LLMCompressor`（自己的客户端），不经过 `HookedModel`，所以不进审计，也不计入对话令牌上限 | `app/core/dependencies.py`、`app/services/memory_compression/llm_compressor.py` |

结论：现在没有已知的漏洞，因为唯一的生产入口每次都装了钩子。风险在以后：新入口、新的后台任务，或者直接调用 `LoopRun` 的代码，只要忘了装钩子，确认门控和令牌上限就会悄悄失效，而且不会报错。

## 2. 改动一：没装钩子就拒绝（必做）

- `hooks.py`：
  - 上下文变量的默认值改为一个内部的“未安装”标记，不再是 `EMPTY`。
  - `current_hooks()` 不变：没装时仍返回 `EMPTY`。观察、追踪、进度回调都照旧，不用改调用方。
  - 新增 `installed_hooks()`：没装时返回 `None`。
  - `HookedModel.generate` 改用 `installed_hooks()`。没装时抛 `PolicyDeniedError('hooks_not_installed', 原因)`，什么都不发给服务商。
  - `HookedTool.invoke` 同样处理：没装时不执行，返回“未执行：原因”，和被门控拒绝的结果形状相同。
  - **原因要写清楚**（用户 2026-11-14“给出详细拒绝原因”）。宿主写的英文原因要说明三件事：拒绝了什么（模型调用的用途，或工具名）；为什么（这次运行没有装钩子，确认门控、对话令牌上限和审计都管不到这次调用）；怎么改（通过 `RagAgentService` 运行，或者用 `using_hooks(...)` 包住直接调用；确实不要钩子时装 `EMPTY`）。同样的内容写一条警告日志，只记用途或工具名，不记参数。
  - 新增失败类型 `hooks`。`LoopRun` 看到 `hook == 'hooks_not_installed'` 的拒绝时，用它代替 `policy`，任务结果写明上面的原因。`FAILURE_MESSAGES['hooks']` 的中文说明：“这次运行没有装上钩子，权限确认、令牌上限和审计都无法生效，为安全起见拒绝了模型和工具调用，任务停止。这是程序接线的问题，不是模型的问题。”命令行和网页按现有方式显示失败类型的中文说明。
  - `docs/agent_runtime.md` 的失败类型对照表加上 `hooks` 一行。
  - 装了空的 `HookSet` 仍然直接放行，所以默认运行的速度和请求都不变。
- `rag_agent_service.py`：`_run_hooks` 现在在没有服务钩子时取 `current_hooks()`，改为 `installed_hooks() or EMPTY`。意思不变，只是写明“服务就是安装者”。
- 测试：
  - 改：上面实测到的 7 项测试，改为显式 `using_hooks(EMPTY)`，或者改测新行为。其中 `test_hooks.py`、`test_trace.py` 各有一项专测“没装钩子就直接放行”，改成测“装了空的就直接放行”。
  - 新增：没装钩子时，模型调用抛 `PolicyDeniedError`，底层模型一次都没被调用，原因里有用途和改法；工具调用不执行，底层工具一次都没被调用，返回的错误里有工具名和改法；走完整的 `LoopRun` 时，任务以 `hooks` 失败，结果里有原因；各写一条警告日志，日志里没有参数内容。
  - 新增：服务的正常路径（命令行、网页、评测）在没有任何钩子配置时仍能跑完，即服务一定会装上钩子。

## 3. 改动二：网页确认时显示要做什么（要你选，见第 7 节第 1 问）

建议做法（第 7 节选项 a）：

- `HookedTool` 发给**门控**的事件多带一个 `args_preview`。**观察者**收到的事件不变，所以审计文件、追踪和评测记录里仍然没有模型写的内容，D-059 对观察者的约定不变。
- `args_preview` 由宿主生成，有上限：
  - `write_file`：`path`、`overwrite`，以及 `content` 的前 20,000 个字符，超出部分只记字符数；
  - `run_python`：`path` 和 `args`（最多 16 项，每项最多 200 个字符）；
  - `run_tests`：`path`；
  - 其他工具：字符串参数各取前 2,000 个字符。
- `ApprovalGate` 把 `args_preview` 放进 `approval_required` 事件。这类事件只存在内存里（保留 30 分钟，`WEB_EVENT_RETENTION_SECONDS`），只发给带令牌的网页，不写盘。实现时要再核对一遍。
- `app.js` 的确认卡片（用户 2026-11-14：上限 2 万可以，但默认别显示太长，展开再看全部）：
  - 默认只显示内容的前 12 行，并且不超过 800 个字符；
  - 内容更长时，下面一个“展开全部（共 N 字符）”的按钮，展开后在一个有最大高度、可滚动的区域里显示预览里的全部内容（最多 2 万字符），可以再收起；
  - 预览本身被截断时（原内容超过 2 万字符），展开区最后写明“还有 N 字符未显示”；
  - 全部用 `textContent` 显示，不当作 HTML 解析。顺带删掉 `approvalText` 里过时的 `args.code`（三个要确认的工具都没有 `code` 参数）。
- 新增决策 D-080：门控事件可以带有上限的参数预览，观察者的事件永远不带。

## 4. 改动三：把“工具调用逐个执行”固定下来（必做，很小）

- 新增测试：模型一轮返回两个工具调用时，第二个要等第一个结束才开始，并且两个调用都先各自过门控。单一循环和团队成员用的是同一个 `AgentLoopRunner`，测一处就够了。
- 在 `HookedTool.invoke` 和 `ApprovalGate` 旁边加一行注释：“先过门控、再调用”依赖工具调用逐个执行；以后要并行执行工具时，必须重新检查门控。

## 5. 不改的

- 后台摘要（`LLMCompressor`）不纳入钩子。它不带工具，没有副作用，而且运行在对话结束之后，那时没有“这一次运行”的钩子可用。在改动一之后，如果把它接到 `HookedModel` 上，反而会因为没装钩子而被拒绝。只在 `hooks_design.md` 里写明：它不计入对话令牌上限，也不进审计。
- `run_python_code` 继续不需要确认（D-028）。
- 钩子的接口、事件名、事件字段（除了门控事件多一个 `args_preview`）、`summarize_arguments`、审计格式（`AUDIT_VERSION`）都不变。
- `.env`、模型默认值、依赖都不改。

## 6. 实现分块和验收

| 块 | 内容 | 验收 |
|---|---|---|
| H-1 | 改动一（含失败类型 `hooks`） | 第 2 节的测试；`init.ps1` 通过 |
| H-2 | 改动三 | 第 4 节的测试 |
| H-3 | 改动二（按第 7 节的回答） | 预览上限、观察者事件不带预览、确认事件带预览的测试；`test_web_approvals.py` 通过；网页卡片手动看一次 |
| H-4 | 文档：D-080、`hooks_design.md` 第 8、12 节、文档索引、交接分支的 `feature_list.json` | Claude 来做 |

- Codex 实现，Claude 审查。先派 H-1 和 H-2（一次任务），审查通过后再派 H-3。
- 不需要真实模型评测：请求逐字节不变，结果由确定性测试保证。H-3 的网页卡片要手动看一次，这需要启动网页并真实跑一次会写文件的任务，到时另请你授权。

## 7. 需要你确认的问题

1. **确认时显示多少内容**：
   - (a) 按第 3 节，最多显示 20,000 个字符，可以展开查看（建议）；
   - (b) 只显示前 40 行；
   - (c) 不改，继续只显示路径和长度，在文档里写明这是盲批。

   建议选 (a)：大多数文件不到 2 万字符，能完整看到；预览只给确认门控，不进审计和追踪。
2. **没装钩子时直接报错，还是只记警告**：按第 2 节，没装钩子的模型调用和工具调用一律拒绝。另一种做法是照旧放行，只记一条警告。建议拒绝，因为警告容易被忽略，而目前唯一的生产入口每次都装了钩子，拒绝不影响现有运行。
3. **由谁实现**：按约定，明天 Codex 恢复额度后交给它，可以吗？还是由 Claude 现在实现？

**用户回答（2026-11-14）**：
1. 上限 2 万字符可以，但默认显示要缩短，展开再看全部（第 3 节已按此改）；
2. 拒绝，并给出详细的拒绝原因（第 2 节已按此改，新增失败类型 `hooks`）；
3. Codex 现在有额度，直接交给它实现。

## 8. 实现记录（2026-11-14）

- H-1、H-2（Codex，Claude 审查）：`hooks.py` 的“未安装”标记、`installed_hooks()`、拒绝原因和警告日志；失败类型 `hooks`（`runtime.py`、`loop_run.py`、`docs/agent_runtime.md`）；`_run_hooks` 写明服务是安装者。Codex 还发现 `LoopRun.run` 原来会把 `current_hooks()` 的 `EMPTY` 装上，从而绕过拒绝，已改为没装时不装，进度回调另行转发。7 项原测试改为显式装 `EMPTY`，两项“没装就放行”的测试改为“装了空的就放行”。
- 审查时补的一处（Claude）：团队入口 `DelegatingLoopRunner.run` 原来把 `current_hooks()` 加上团队完成门控后装上，没装钩子的团队运行会因此被放行。改为没装时不装，完成门控交给负责人直接检查（`AgentLoopRunner` 新增 `finish_gates`）；新增 `test_team_without_run_hooks_does_not_install_its_gate_as_run_hooks`，在修复前的代码上会失败。
- H-3（Codex，Claude 审查）：`preview_arguments`、门控事件的 `args_preview`、确认事件转发、确认卡片的折叠和展开、新测试 `tests/test_hook_previews.py` 和 `test_web_approvals.py` 一项。核对过 `app/api/runs.py`：运行事件只在内存里，经带令牌的接口提供，不写盘。审查时改了两处显示：是否覆盖显示为“是/否”，字符数加千位分隔。
- 验证：`init.ps1` 通过，1855 项通过、16 项跳过；`tests/test_loop_run_equivalence.py` 未改动并通过（请求逐字节不变）。仓库没有前端测试框架，没有新增依赖；确认卡片还没在网页上实际看过，要启动网页并真实跑一次会写文件的任务，需另行授权。
