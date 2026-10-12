# Agent Runtime

The runtime assembles existing components in this order:

```text
Goal -> WorkingMemory -> Planner -> Executor.accept_plan()
     -> next pending Step -> StepExecutor -> ToolCallingStepRunner
     -> ToolRegistry -> local RAG / local tool / MCPToolAdapter -> MCPClient
     -> successful commit / failed replan / blocked question
     -> WorkingMemory.complete_task() after every plan step is complete
```

## Responsibilities

- `open_tool_registry()` registers `CurrentTimeTool`, `KnowledgeRegistryTool`,
  caller-supplied local tools and tools discovered from stdio MCP servers. It
  owns live MCP contexts in an `AsyncExitStack`. Run the entire task in that
  context and the same asyncio task. Duplicate tool names fail initialization;
  all connections opened so far are closed.
- `KnowledgeRegistryTool` calls the existing `asearch_knowledge()` directly.
  Query rewriting, dense/BM25/hybrid retrieval and reranking are unchanged. It
  preserves evidence and the existing dataclass citations, distinguishes
  `ok`/`empty`/`error`, and registers successful evidence bundles in `Resources`.
  An empty result is a successful search with no factual evidence; an error
  becomes a failed tool result. Neither registration nor loading changes the
  selected planning context or invalidates an active execution token.
- `PlanningModel` adapts the existing `ToolCallingModel` to Planner's
  `generate(prompt) -> JSON string` contract. It supplies no executable tools.
  The real inventory is pinned as task data so Planner can refer to available
  tools without invoking them.
- `Planner` still proposes plans; `Executor` still validates freshness and
  installs them. Runtime chooses when to plan/replan and which pending step to
  execute. `StepExecutor` owns starting, validating and committing each step.
- `ToolCallingStepRunner` still owns the bounded tool loop. Runtime enables
  `require_step_outcome=True`; its final model response must be exactly:

  ```json
  {"status": "succeeded", "content": "The requested step result", "reason": null}
  ```

  Status can be `succeeded`, `failed` or `blocked`. Failed/blocked outputs require
  a non-empty reason. Missing, unknown or duplicate fields/statuses and plain
  failure prose are rejected as step failures. Native tool calls remain native
  tool calls. Existing low-level callers retain the default text-output mode.

## Outcomes and limits

Successful outputs alone are submitted to `commit_step_result()`. Tool evidence
is saved as the latest observation, and step summaries become valid intermediate
results. These are available to later steps and replacement plans of the same
goal. A tool failure may be recovered by another tool call in the same step;
the model must explicitly report whether the required step output was achieved.

Failed outputs and tool exceptions are recorded with `record_failure()`, return
the task to planning, and trigger a replacement plan for the remaining work.
The failure record includes the failed output and returned tool evidence when
available. An exception before a tool returns may have no returned evidence.
Runtime does not retry individual side-effecting calls automatically.

Blocked outputs mark the step/task `BLOCKED`, record a blocking question and
release the active step. They publish no successful intermediate result. Runtime
does not run dependent work or repeatedly replan until the question is answered.
`resolved_questions` carries answers into both planning and execution contexts.

Default bounds are 16 step executions, including failures and timeouts, and 2
replacement plans. Initial planning is not a replan. Each planning/execution
attempt has a 60-second timeout; the existing MCP client also bounds individual
calls. Tool-loop limits remain 4 rounds and 8 calls per step. A failed initial
plan, a failed replan, contract errors or exhausted bounds terminate the task
as `FAILED`, preserving history. Cancellation terminates the owned task, releases
the active step and propagates cancellation; the registry context closes tools.

Counts persist when a runtime pauses and resumes. All plan steps must be
completed, no current-goal blocker may remain, and resource-context checks must
pass before `complete_task()` is called. Results rejected as stale never overwrite
newer execution state. Runtime access is serialized; external callers must not
mutate WorkingMemory during an active run.

## Memory and blocked-task resume

WorkingMemory remains task scoped. ConversationMemory remains session scoped;
the runtime imports its summary and last three turns as bounded historical data,
then appends one complete user/assistant turn for each changed reported outcome.
Repeated reads of an unchanged terminal/blocked result do not add duplicate turns.
RAG query rewriting receives recent conversation and valid task results.

`RagAgentService` runs each query as a new task and retains ConversationMemory by
session. Queries for the same session are serialized. It uses the new Runtime
instead of the former `create_agent`/LangChain MCP execution path.

For explicit blocked-task resume, keep the Runtime and registry context alive:

```python
from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.models.taskstate import TaskState, TaskStatus
from app.models.working_memory import WorkingMemory
from app.tools.tool_loader import open_tool_registry

# planner_model follows Planner's text contract; tool_model follows ToolCallingModel.
async def execute_goal(goal, planner_model, tool_model, servers, get_answer):
    memory = WorkingMemory(TaskState("user", goal))
    async with open_tool_registry(memory, mcp_servers=servers) as registry:
        runtime = AgentRuntime(Planner(planner_model), tool_model, memory, registry)
        result = await runtime.run()
        if result.status is TaskStatus.BLOCKED:
            for question in memory.task_state.scratchpad.open_questions:
                if question.blocking and question.resolved_at is None:
                    answer = await get_answer(question.content)
                    memory.task_state.resolve_question(question.question_id, answer)
            result = await runtime.run()
        return result
```

## Current limits

- This is sequential, process-local execution. No persistence, parallel steps,
  cross-process recovery or transactional rollback of external tool side effects
  is implemented. Timeout/cancellation may occur after a side effect has happened.
- The default service uses stdio Filesystem/WebSearch MCP and local RAG/time
  tools. Historical HTTP CLS/monitor configuration and the unused `aiops`
  experiment modules are not wired into this runtime. Legacy tool exports remain
  available for compatibility; they are not used by the service's execution path.
- `query_stream()` retains an event interface but emits final content/status,
  not per-token output. Injected service models now follow `ToolCallingModel`,
  rather than LangChain `ChatQwen`. `query_result()` is the status-aware API.
- Service/CLI queries start new tasks. The low-level Runtime supports blocked
  resume; the service does not persist paused tasks or expose a resume endpoint.
  CLI history lasts only for that process.
- The model declares step success under a validated protocol; this is not an
  independent semantic verifier. Final content combines valid successful step
  results, without an extra synthesis model call. Planner should include a final
  synthesis step when the goal needs one.
- Tests use real stdio MCP subprocesses and the real provider-message adapter,
  with deterministic model, RAG and Tavily responses. Live DashScope, Tavily,
  Milvus and local reranker services were not exercised.
