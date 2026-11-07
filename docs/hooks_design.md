# Hooks 设计稿

日期：2026-11-07。状态：**已确认，未实现**。来源：用户 2026-11-07“先写 hooks 设计稿”；第 11 节五个问题用户 2026-11-07 答“都按推荐”（D-058）。

## 1. 目标和范围

给智能体留出固定的扩展点，以后要加“写文件前先问用户”“记审计日志”“按对话封顶费用”这类行为时，写一个 hook 挂上去即可，不用再改规划、执行和工具调用这些核心代码。

第一版只做：

- 一组固定的事件点，覆盖任务、规划、模型调用、工具调用和步骤。
- 两种 hook：只读的**观察型**，可以拒绝的**拦截型**。
- 四个内置 hook：进度推送（替换现有的 `on_event`）、审计日志、对话费用上限、网页里的人工确认。
- 普通模式和团队模式都经过同一套 hook。

第一版不做：从配置文件动态加载第三方 hook 模块、交给外部 shell 命令处理事件、在 hook 里改写工具参数或模型请求。

## 2. 现在的情况

扩展性逻辑散在各处，并且都是写死的：

| 行为 | 现在的位置 | 问题 |
| --- | --- | --- |
| 进度推送（`plan`、`step` 记录） | `app/agent/runtime.py` 的 `_emit` 和 `run(on_event=...)`（D-045） | 只有两种事件、只能挂一个回调，看不到模型调用和单次工具调用 |
| 令牌和时间预算 | `app/agent/run_meter.py`，由 `runtime.py` 和 `openai_compatible_tool_calling_model.py` 检查 | 只管一次请求，没有按对话累计的上限 |
| 每步的副作用记录 | `app/agent/side_effects.py` 的 `SideEffectWatch`（D-032） | 这是步骤语义的一部分，**保留不动** |
| 敏感文件过滤、写入目录限制 | `app/tools/filesystem/file_access.py`（D-029） | 这是安全底线，**保留在工具内部** |
| 工具结果截断和归档 | `app/services/context_manager.py`（D-052） | 属于上下文管理，不改 |
| 写文件、长期记忆是否可用 | 网页每条消息的选项（D-051） | 只能整体开或关，不能逐次确认 |

缺的是：调用工具前没有人工把关；每次运行没有一份完整的结构化记录；没有按对话累计的费用控制。

## 3. 设计原则

1. **安全底线不交给 hook。** 敏感路径、写入目录、沙盒限制仍然在工具内部检查。hook 只能在此基础上**再加限制**（拒绝），不能放宽。
2. **观察型 hook 出错不影响任务。** 只按类型名记一条警告，和现在的 `_emit` 一样。
3. **拦截型 hook 出错或超时都当作拒绝**（遇错即拒）。
4. **事件内容只放宿主生成的数据。** 不放工具输出原文、模型回复原文、写入文件的内容或异常信息，沿用 D-045 的约束。
5. **第一版的 hook 只能拒绝，不能修改。** 改写参数会让“模型以为调用了什么”和“实际执行了什么”对不上，排查起来很困难。

## 4. 事件点

| 事件 | 什么时候 | 主要字段 | 可拦截 |
| --- | --- | --- | --- |
| `task_start` | 一次运行开始 | `task_id`、`session`（哈希）、`mode`、`options` | 否 |
| `plan` | 规划被接受 | `revision`、`steps`、`step_ids` | 否 |
| `before_model` | 每次请求模型之前 | `purpose`（规划、步骤、收尾、修复、路由）、`message_count`、`tool_count`、预估输入令牌数 | **是** |
| `after_model` | 每次模型返回之后 | 输入、输出、命中缓存的令牌数，用时，是否请求了工具 | 否 |
| `before_tool` | 每次执行工具之前 | `tool`、`call_id`、`args_summary`（只放路径、长度这类摘要）、`side_effect`（是否有副作用） | **是** |
| `after_tool` | 工具返回之后 | `tool`、`call_id`、`success`、`chars`、用时 | 否 |
| `step` | 一步结束 | 和现在的 `step_log` 一条相同 | 否 |
| `task_end` | 一次运行结束 | `status`、`failure_kind`、`usage` | 否 |

关于 `args_summary`：每个工具声明自己的摘要规则。例如 `write_file` 只给路径和字节数，`read_file` 给路径和行范围，`search_files` 给模式长度和路径。没有声明规则的工具，只给参数名列表。

## 5. 接口

```python
class Hook(Protocol):
    name: str
    def observe(self, event: HookEvent) -> None: ...       # 观察型；同步，必须很快

class Gate(Protocol):
    name: str
    events: frozenset[str]                                 # 只能是 before_model / before_tool
    async def check(self, event: HookEvent) -> Decision: ...

@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str | None = None        # 宿主写的拒绝原因，会交给模型或显示给用户

class HookSet:
    """一次运行的全部 hook；不可变，由服务在运行开始时组装。"""
    def emit(self, event) -> None: ...                     # 依次通知观察型 hook
    async def gate(self, event) -> Decision: ...           # 依次询问拦截型 hook，第一个拒绝就停
```

- 拦截型 hook 有单次超时（默认 5 秒），人工确认另有超时（见第 8 节）。
- 观察型 hook 必须是同步、不阻塞的。要做耗时工作（写文件、网络），由 hook 自己放进后台队列。

## 6. 接入位置

为了让普通模式、团队模式、规划和收尾请求都经过 hook，在两个最底层的位置包一层，而不是改各个执行器：

1. **工具：注册时逐个包装。** 在 `app/tools/tool_loader.py` 的 `open_tool_registry` 里，把每个工具对象换成 `HookedTool`：它的 `invoke` 先触发 `before_tool`，再执行原工具，然后触发 `after_tool`。
   - 团队模式给每个成员建注册表时，用的是 `self.registry.get(tool_name)` 取出的工具对象（`app/agent/team_v2.py`）。在注册表那一层拦截会被绕过，**包在工具对象上才能覆盖团队成员**。
2. **模型：包在最底层的模型对象上。** `RagAgentService._models` 产出的模型先包一层 `HookedModel`，规划（`PlanningModel(ContextManagedModel(model))`）、步骤、团队成员、路由都经过它。`purpose` 由调用方通过上下文变量标明，做法和 `run_meter` 的当前计量器一样。
3. **任务和步骤事件：** `runtime.py` 里现有的 `_emit` 改为调用 `HookSet.emit`；`run(on_event=...)` 保留，内部转成一个观察型 hook，现有调用方不用改。

`HookSet` 在一次运行期间通过上下文变量传递，和 `metering()` 的做法相同，不需要在每个函数之间传参数。

## 7. 被拒绝时怎么处理

- **工具被拒绝**：不执行工具，返回 `ToolOutput(success=False, error="not executed: <原因>")`。模型能看到这个结果并调整做法；它也和失败的工具一样计入调用次数和轮数，所以模型不会无限重试。
- **模型请求被拒绝**（例如超出费用上限）：这次运行按预算用完处理，`failure_kind` 为 `policy`，显示“已达到这个对话的费用上限”这类中文说明。
- 每次拒绝都记一条 `after_tool` 或 `after_model`，标明 `denied_by`（拒绝它的 hook 名）。

## 8. 人工确认（网页）

这是第一个拦截型 hook，也是加 hooks 的主要动机。

1. 规则：在网页上，`write_file`、`run_python`、`run_tests` 这些有副作用的工具（`app/agent/side_effects.py` 的 `SIDE_EFFECT_TOOLS`）执行前要用户确认。
2. 过程：
   - 拦截型 hook 发出 `approval_required` 进度事件，内容是工具名和参数摘要，例如“写入 `output/todo.html`，2.3 KB”。
   - 这次运行在当前位置**等待**。网页显示“允许”“拒绝”“本次运行都允许”三个按钮，用户点击后通过 `POST /api/runs/{run_id}/approvals` 回传。
   - 默认等待 300 秒，超时按拒绝处理。
3. 取消运行时，正在等待的确认按拒绝处理。
4. 命令行和评测不启用这个 hook。命令行已经要求显式给出 `--write-root`；评测需要无人值守。

## 9. 第一批内置 hook

| hook | 类型 | 做什么 |
| --- | --- | --- |
| `ProgressHook` | 观察型 | 把 `plan`、`step`、`approval_required` 转给网页，取代现在的 `on_event` |
| `AuditHook` | 观察型 | 每次运行写一份 JSON Lines 审计记录，存放在这个对话的归档目录里，随对话一起删除；内容只有第 4 节列出的字段 |
| `CostCapGate` | 拦截型 | 按对话累计令牌数，超过 `CONVERSATION_MAX_TOKENS` 就拒绝之后的模型请求；不设置就不启用 |
| `ApprovalGate` | 拦截型 | 第 8 节的人工确认，只在网页上启用 |

## 10. 新增配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `HOOKS_AUDIT` | `true` | 是否写审计记录；没有对话归档目录时不写 |
| `CONVERSATION_MAX_TOKENS` | 空 | 每个对话累计令牌上限；空表示不限 |
| `WEB_APPROVAL_TOOLS` | `write_file,run_python,run_tests` | 网页上需要确认的工具；空表示都不确认 |
| `WEB_APPROVAL_TIMEOUT_SECONDS` | `300` | 等待用户确认的秒数，超时按拒绝处理 |

hook 的组装写在代码里（服务和网页后端），**不从 `.env` 读模块名再动态导入**，免得改一个配置项就能加载任意代码。

## 11. 已定的问题（用户 2026-11-07“都按推荐”）

1. **拦截型 hook 能不能修改参数或请求？** 推荐：第一版只能拒绝（理由见第 3 节第 5 条）。
2. **人工确认用哪种方式？**
   - 推荐**运行内等待**：工具还没执行，用户点完按钮后从原地继续，不用重新请求模型。
   - 另一种是复用现有的“任务停下、回答后恢复”：实现简单，但恢复时整步要重跑，多花一次模型调用，而且恢复时模型可能换一种做法。
3. **哪些工具默认要确认？** 推荐：网页上 `write_file`、`run_python`、`run_tests`；命令行和评测都不确认。
4. **要不要支持外部 shell 命令作为 hook？** 推荐：第一版不做，等 Python 接口稳定后再加。
5. **审计记录存在哪里？** 推荐：对话归档目录，随对话一起删除；不单独开目录，免得留下孤立的记录。

## 12. 测试

- 事件顺序：单步任务依次是 `task_start`、`before_model`、`after_model`、`plan`、`before_model`、`before_tool`、`after_tool`……`step`、`task_end`；团队模式成员的工具调用也出现。
- 观察型 hook 抛出异常，结果不变。
- 拦截型 hook 拒绝时，工具没有被执行，模型收到“未执行”的结果；拦截型 hook 超时或抛出异常时按拒绝处理。
- 事件里不出现工具输出原文、模型回复原文、写入的文件内容或异常信息（用带标记的假数据检查）。
- 人工确认：允许、拒绝、本次都允许、超时、取消运行这五种情况。网页接口用现有的测试客户端检查。
- 费用上限：同一对话的第二次运行在超过上限后被拒绝，`failure_kind` 为 `policy`。
- 现有 `on_event` 测试全部照常通过。

## 13. 实施顺序

1. `HookSet`、事件类型和两种接口，加上 `HookedTool`、`HookedModel`；`_emit` 改为走 `HookSet`。行为不变，现有测试全部通过。
2. `AuditHook` 和 `CostCapGate`。
3. `ApprovalGate`、网页接口和页面上的确认按钮。
4. 跑一次调参集，确认没有回退；网页上真实走一遍人工确认。

每一步都单独记一条功能清单和决策记录。

## 14. 实现时的调整

- 第 1 步（D-059，2026-11-07）：参数摘要用统一规则（路径、行号、上限这类短值保留，其他文字只记长度，列表只记条数），不按工具逐个声明；`task_start` 暂不带会话哈希和执行模式，等第 2 步审计时由服务补上；格式修复请求暂时标为 `step`。
- 第 2 步（D-060，2026-11-07）：审计记录在运行结束时同步写一次，不放后台队列；执行模式写在审计记录头里；会话由文件所在目录体现；`auto` 模式选择执行方式的那次模型调用不计入对话令牌上限；被拒绝的运行用新的失败类型 `policy`。
- 第 3 步（D-061，2026-11-07）：等待时间不计入时限的做法定为——步骤和团队的时限登记为可暂停，等待前先往后推一个最长等待时间，等完后收回没用到的部分；确认事件只进网页的运行事件，不进审计记录；回答后的卡片留在执行过程里。

## 15. 风险

- **逐次调用都触发事件，会增加开销。** 观察型 hook 是同步的，只做内存操作，开销很小；写文件放在后台。
- **人工确认让一次运行可能挂起很久。** 等待期间会继续计时，因此要么等待时暂停时间预算，要么把确认超时设得比单步超时短。实施第 3 步时定下来。
- **团队模式里多个成员同时请求确认**：确认请求按顺序排队，每次只显示一个。
- **`purpose` 依赖调用方标明。** 漏标时记为 `unknown`，不影响拦截，只影响审计记录的可读性。
