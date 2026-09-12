# Executor V1：接收 Planner 响应

本阶段将 Executor 定义为新版 `app.agent.planner.Planner` 的响应接收方。
Planner 继续只返回不可变的 `PlanProposal`；Executor 校验并安装该提案，
返回不可变的 `PlanAcceptance`。不在 Planner 响应里嵌入有副作用的执行器实例。
旧版 `app.agent.aiops` 工作流独立保留。

## 接入方式

```python
from app.agent.planner import Planner
from app.agent.executor import Executor
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory

wm = WorkingMemory(TaskState("user-1", "检查报告并交付结论"))
planner = Planner(model)  # model 提供 async generate(prompt) -> JSON 字符串
executor = Executor(wm)

wm.task_state.start_planning()  # 必须在生成上下文之前调用
proposal = await planner.plan(planner.build_context(wm))
receipt = executor.accept_plan(proposal)
# receipt.next_step 是候选步骤，仍为 PENDING，并未执行。
# receipt.blocking_questions 提供当前目标尚未解决的阻塞问题。
```

`PlanProposal` 新增必填 `task_id`，由 Planner 从上下文写入，模型不生成它。
手动构造提案的调用方也需要提供此字段。仅比较目标版本和状态版本无法识别
不同任务中恰好相同的版本号，因此接收方同时校验这三个字段。

接收流程：

1. 校验类型、任务归属、目标版本、基础状态版本及步骤内容。
2. 首次计划通过 `TaskState.set_plan()` 安装；已有计划版本通过 `replan()` 替换。
3. 状态机拒绝执行中或终态的计划替换；校验失败不修改内存。
4. 返回安装后的版本、步骤、下一候选步骤和当前目标阻塞问题。
   安装本身增加状态版本，因此重复接收同一提案会被拒绝。

重规划仍由调用方显式调用 `planner.replan(planner.build_context(wm))`，
再传给同一个 `executor.accept_plan()`。历史结果及失败记录由 TaskState 保留。

## 后续执行逻辑

下一阶段可在 Executor 上增加单步执行入口：先读取最新快照并核对计划版本，
确认下一 pending 步骤和阻塞条件，再调用 `WorkingMemory.begin_step()` 获取执行标识。澄清步骤可用于解除
阻塞，依赖该答案的工作必须等待问题解决；V1 的候选步骤不是执行授权。

执行入口向注入的 runner 传递当前步骤、约束、选中的资源上下文和同目标有效
结果。runner 负责实际模型或工具调用，返回观察、结果及来源引用。
成功输出封装为 `StepExecutionResult`，通过 `WorkingMemory.commit_step_result()`
统一校验来源和执行标识，写入证据并完成步骤；
失败则调用 `add_failure()`，返回规划阶段，由调用方决定是否重规划。
取消、超时和写回期间状态变化需要在该阶段定义处理规则。

所有步骤完成后仍需由调用方验收并调用 `complete_task()`。
V1 不自动调用工具、重试、重规划或宣布任务完成。

与 WorkingMemory 一致，调用方需串行访问共享状态。状态版本仅覆盖 TaskState，
不检测资源单独变化；未来涉及工具副作用时，需要增加资源版本或快照一致性检查。
