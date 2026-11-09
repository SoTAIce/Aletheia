# 规划模式：执行步骤时不再重复发送工具清单（修改方案）

日期：2026-11-08。状态：**已实现**（tool-list-dedup-001，D-069），确定性测试和调参集规划模式真实评测（11/11）都已通过；原方案作为 tool-list-dedup-plan-001 保留。Codex 写完代码和测试后额度用完，审查、测量和记录由 Claude 完成。

## 1. 背景

- 架构梳理（[architecture_review.md](architecture_review.md) 第 3 节问题 7）指出：运行时把工具清单（名字和说明）钉进任务状态，同样的工具又作为原生工具定义发给模型，每次请求都发了两遍。
- 单一循环在 D-062 时已经在自己的上下文里去掉了这份清单。规划模式仍然会用到：网页上可以逐条选"按计划"，命令行和评测也能选；就算以后默认改成循环，它仍是一个常用选项（用户 2026-11-08 指出）。所以规划模式也要去掉这份重复。
- 第 0 阶段（D-066）把这一项留到了现在，因为它会改变发给模型的内容，需要真实评测来判断好坏。

## 2. 核对结果（2026-11-08，HEAD `9902cb0`）

| 位置 | 是否也带原生工具定义 | 钉住的清单是不是重复 | 结论 |
|---|---|---|---|
| 运行时 `app/agent/runtime.py` 第 279～282 行：任务开始时把 `[{name, description}]` 钉进任务状态，来源是 `"available_tools"` | — | — | 保留：规划器要用 |
| 规划器 `app/agent/planner.py`：`_build_prompt` 把 `asdict(PlannerContext)`（含 `pinned_contexts`）整体序列化 | **不带**，规划调用不传工具 | 不重复，这是规划器知道有哪些工具的唯一途径 | **不改** |
| 执行步骤 `app/agent/tool_calling_step_runner.py` 第 155～159 行：`asdict(context)` 按 `CONTEXT_FIELD_ORDER` 整体序列化，`pinned_contexts` 原样带上 | **带**，每次请求都传原生定义；收尾请求用 `ToolsShownOnly`，定义也照样带着 | **重复** | **去掉** |
| 团队负责人 `app/agent/team_v2.py` 第 587 行：`asdict(context)` 整体序列化 | 只带委派类工具；系统提示（第 261 行）只列可分配的工具名，没有说明 | 不完全重复：清单是负责人唯一能看到工具说明的地方 | **不改**（要改的话需要团队评测，另行决定） |
| 单一循环 `app/agent/agent_loop.py` 第 125 行 | 带 | 已经去掉（D-062） | 改用下面的共用常量，行为不变 |

另外：
- 只读任务里，钉进去的清单来自已经收回写入工具的注册表（运行时拿到的是 `task_registry(...)` 之后的注册表），和步骤里的原生定义一致。所以去掉清单不会让步骤少知道任何工具。
- 除了 `tests/test_agent_loop.py`，没有测试断言步骤上下文里一定有 `available_tools`。

## 3. 要做的修改

### 3.1 共用一个来源名常量

- 在 `app/agent/step_models.py` 加一个常量：`TOOL_LIST_SOURCE = 'available_tools'`，并加一行注释：这是运行时钉住的工具清单的来源名，规划器要用；带原生工具定义的请求不需要它。
- `runtime.py` 第 282 行的 `"available_tools"` 改用这个常量。
- `agent_loop.py` 第 125 行的字符串也改用这个常量，行为不变。

### 3.2 执行步骤的上下文去掉这份清单

在 `tool_calling_step_runner.py` 构造步骤消息的地方（第 155～159 行附近，`fields = asdict(context)` 之后、`json.dumps` 之前），把 `pinned_contexts` 里 `source == TOOL_LIST_SOURCE` 的项过滤掉。其余钉住内容（对话记忆、长期记忆等）的顺序和内容保持不变，`CONTEXT_FIELD_ORDER` 也不变。写法参照 `agent_loop._context_message`。

注意以下几点：
- **只在序列化时过滤**，不要改 `StepExecutionContext`、`StepExecutor.build_context` 或任务状态本身。规划器和团队负责人仍然要读到完整的 `pinned_contexts`。
- 过滤后 `pinned_contexts` 如果为空，保留空列表，不要删掉这个键。这样字段集合不变，和缓存布局（D-057）的测试一致。
- 系统提示里 "consider pinned_contexts" 这类说法不用改。

### 3.3 不改的地方

- 规划器的提示和上下文，团队负责人和成员的上下文，单一循环的行为。
- 工具定义的格式、`ToolsShownOnly`、收尾逻辑（D-053、D-057）。
- 不改提示词措辞，不改模型默认值、依赖和 `.env`。

## 4. 测试（确定性测试，不调用真实模型）

在 `tests/agent/test_tool_calling_step_runner.py`、`tests/test_prompt_cache_layout.py` 附近，或新建 `tests/test_step_tool_list.py`，覆盖以下几点：

1. 任务状态里钉了 `TOOL_LIST_SOURCE` 和对话记忆时，步骤请求的第一条用户消息里，`pinned_contexts` 只剩对话记忆，原生工具定义照常传给模型。
2. 同样的任务状态下，规划器的提示里**仍然**带着工具清单。这一项防止以后误删规划器的清单。
3. 只钉了工具清单时，步骤上下文里的 `pinned_contexts` 是空列表，键还在。
4. 收尾请求（`ToolsShownOnly`）的上下文里同样没有工具清单。
5. 运行时任务开始时，仍然用 `TOOL_LIST_SOURCE` 钉住清单。
6. 已有的缓存布局测试（`tests/test_prompt_cache_layout.py`）照常通过：同一任务的前后两步，请求开头仍然相同。

再用假模型量一次改动前后，一次执行步骤请求的用户消息有多少字符（可以参考会话暂存目录里的做法：用脚本模型重建请求），把数字写进记录。不要为此调用真实模型。

完成标准：`init.ps1` 通过，测试数在 1639 passed、16 skipped 的基础上增加。

## 5. 改完以后要用户另行授权的真实验证

这项改动会改变执行步骤发给模型的内容，确定性测试只能证明"清单没了"，不能证明效果没变差。Codex **不要**跑真实评测。审查通过后，由 Claude 向用户申请：

```powershell
.venv\Scripts\python.exe -B -m bench.run_bench --confirm-live --tasks all --modes single --runs 1 --max-runs 11 --out output\bench-tune-plan-nodup.json
```

对照对象是 `output/bench-tune-single-hooks.json`：规划模式，11/11，52 次调用，1,091,043 令牌。判断标准：通过数不降。令牌数噪声大（每次请求约 1.8 万来自外部，D-063），以评测报告里逐次调用的提示令牌为准。

## 6. 记录（按 AGENTS.md）

- `feature_list.json`：新增 `tool-list-dedup-001`，依赖 `tool-list-dedup-plan-001`。评测要用户授权，所以在真实评测前，状态写 `partial`，evidence 写明"确定性测试通过，真实评测待授权"。实现期间 `active_feature` 设为它；结束时如果状态是 `partial`，改回 `null`。
- `docs/DECISIONS.md`：新增 D-069。写明去掉的是哪一处、为什么规划器和团队负责人保留，以及字符数的前后对比。
- `docs/architecture_review.md`：第 3 节问题 7，以及第 6 节第 0 阶段后面那句"本轮没有做"，改成"执行步骤已去掉（tool-list-dedup-001），规划器和团队负责人按需保留"。
- `progress.md`、`session-handoff.md`：各加一条；交接的下一步写"调参集规划模式真实评测，待用户授权"。
- `AGENTS.md`、`docs/PROJECT_HANDOVER.md`：更新测试数。
- `docs/harness/baseline.md` 和本轮清单：按现有做法记录本轮结束时的未提交状态。

## 7. 给实现者的约束

- 先读 `AGENTS.md`、`session-handoff.md` 顶部最新交接，再读本方案。
- **`app/agent/tool_calling_step_runner.py` 里有用户没提交的中文注释**（已有的未提交差异，约 75 行新增）。只做 3.2 节那一处修改，不要重新格式化文件，不要还原、删除或改写这些注释，也不要暂存这个文件。`app/agent/planner.py`、`app/agent/step_executor.py` 同样有用户的中文注释，这次不需要改，不要碰。
- 用户其余未提交的改动也不要动：`README.md`，以及删掉的 `scripts/benchmark_conversation_memory.py`、`scripts/validate_multi_runner_live.py`、`scripts/validate_summary_context.py`。
- 不调用真实模型，不连真实服务，不安装或下载，不改 `.env`、模型默认值和依赖。
- 不提交、不推送。
- 跑测试时用 `-p no:cacheprovider` 和指向系统 TEMP 的 `--basetemp`，不在项目里留下 `__pycache__` 或 `.pytest_cache`，不往 `data/` 写东西。
- 记录和文档里给人看的文字都用中文，和现有记录一致。
