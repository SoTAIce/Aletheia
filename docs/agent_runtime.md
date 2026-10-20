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

Formatting is handled inside the current planning/step attempt. One complete
JSON Markdown fence can be unwrapped, and malformed JSON permits at most two
model repair requests. Step repairs retain the existing tool history and supply
no tools; unexpected tool calls in a repair are rejected without execution.
These requests do not count as new step executions or replacement plans and
remain inside the existing planning/step timeout. Valid JSON with duplicate
fields, unknown states or inconsistent status/reason values is rejected rather
than rewritten into success. Exhausted format repair terminates as
`StepFormatError`, without replanning or replaying tools.

Tool exceptions (`StepToolError`) and explicit failed outcomes
(`StepBusinessError`) can trigger a replacement business plan. Provider errors
(`StepModelError`) and provider request timeouts (`StepTimeoutError`) terminate
without a new plan. Overall step deadlines retain the existing failed-step
replanning behavior; side effects may already exist. Blocking and cancellation
behavior is unchanged. When the tool budget is reached, the final model request
has no tools; requesting further calls still fails the enforced limit.

`RAG_JSON_MODE=true` requests provider JSON-object output; the application's
field/type/status validators remain authoritative. `RAG_ENABLE_THINKING` is
optional and provider specific. The local Qwen validation uses `false` to reduce
output latency. Unsupported provider options surface as errors, without an
automatic unstructured fallback. Low-level text-mode adapters retain their
original defaults.

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

The service reads `RAG_MODEL_TIMEOUT_SECONDS` (default 30),
`AGENT_STEP_TIMEOUT_SECONDS` (default 60), and
`AGENT_PLANNING_TIMEOUT_SECONDS` (default 60) from the environment. These are
separate bounds: a step may include several model/tool calls. Explicit service
constructor timeouts override the step/planning settings. For slower real API
responses, increase these finite limits rather than disabling them. A request
timeout remains a failed execution and may cause replanning; it never completes
the step successfully.

- This is sequential, process-local execution. No task checkpoint persistence or parallel steps,
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
  CLI history is process-local unless conversation storage is explicitly enabled.
- The model declares step success under a validated protocol; this is not an
  independent semantic verifier. Final content combines valid successful step
  results, without an extra synthesis model call. Planner should include a final
  synthesis step when the goal needs one.
- Tests use real stdio MCP subprocesses and the real provider-message adapter,
  with deterministic model, RAG and Tavily responses. Live validation separately
  requires working provider credentials, Milvus and the local reranker. Passing
  the automated suite does not establish provider availability or long-term
  stability.


## Recoverable Memory and Context (P0/P1)

Conversation persistence is opt-in to preserve existing disk-free callers. Set
`CONVERSATION_STORAGE_DIR=data/conversations`, or pass
`RagAgentService(memory_directory="data/conversations")`. Empty configuration
keeps the previous in-memory mode. The application must own this directory and
supply the authenticated session ID; a session ID is a storage scope, not an
authentication mechanism. Custom archive directories must also be excluded from Git.

The service restores the same session's recent conversation cache when first
accessed. `ConversationMemory` retains 20 complete turns by default, while all
original turns remain in the archive. Immutable turn files are published atomically
before cache eviction. Repeating the same turn ID and contents is idempotent;
conflicting contents fail explicitly. Read/write failures propagate rather than
silently accepting lost history. `clear`, `pop` and `remove` affect the in-memory
view, not the durable archive; this is not a permanent deletion API.

`WorkingMemory` remains the authoritative task state. `ContextManager` projects
copies for Planner and ToolCallingStepRunner and never edits that state. Configure
`CONTEXT_WINDOW_TOKENS`, `CONTEXT_OUTPUT_TOKENS` and `CONTEXT_SAFETY_TOKENS`
(defaults 32768/2048/1024) for the deployed model. Estimates include serialized
messages, system instructions, native tool schemas, tool responses and output
reserve. Agent RAG history additionally budgets the rewrite system prompt, question
and structured-output schema. Counting uses the existing mixed-language estimate,
not a provider tokenizer; provider-specific framing can differ. Protected goals,
constraints and execution state that cannot fit raise `ContextBudgetError`.

Stage summaries require an explicit compressor, reusing the existing
`LLMCompressor`; no additional model is automatically started. For example, inside
an application-owned `AsyncOpenAI` client lifetime:

```python
from app.services.memory_compression.llm_compressor import LLMCompressor
from app.services.rag_agent_service import RagAgentService

compressor = LLMCompressor(client=client, model=application_summary_model)
service = RagAgentService(
    memory_directory="data/conversations",
    conversation_compressor=compressor,
)
result = await service.query_result("Continue the task", session_id="authenticated-session")
```

Archive-backed summaries are generated from bounded original batches (up to 8
turns and an estimated 6000 source tokens by default), never recursively from
previous summaries. Each index entry stores `memory_id`, `title`, `description`,
`storage_ref`, `source_turn_ids` and `session_id`. Summary bodies are external;
lexical lookup selects bounded summary/raw-history metadata for context. The
registered `read_context_artifact` tool reads referenced summaries, raw turns and
tool artifacts in bounded chunks (default 2000, maximum 16000 characters).
Oversized turns remain discoverable as originals even if they cannot fit a summary
batch. Failed, refused or truncated summaries never replace originals. A revision
guard and final commit marker prevent cancelled/stale summary publication.

Large tool results are saved once and represented by call ID, status, preview and
opaque session-scoped reference. Raw execution receipts remain complete. Budget
pressure first removes archived tool previews, then optional historical entries.
A classified provider context overflow permits one stronger projection and one
model retry; it does not invoke tools again. A second overflow or a timeout during
that recovery terminates the task. Ordinary step timeout behavior is unchanged,
so external side effects still require application-level idempotency.

Limits: single-process file coordination; no cross-process exactly-once execution,
paused-task checkpoint recovery, automatic summary merging or semantic retrieval.
Indexes and originals are scanned on disk; very large histories need later scaling
work. Lexical recall can miss paraphrases and summaries can be inaccurate, hence
source references are retained. A tiny synchronous summary commit-marker write
avoids an await/cancellation publication race; other archive I/O runs in workers.
Cancellation may leave invisible draft artifacts, which are retained safely.
Stored ConversationTurns cover user/final replies, not a full execution checkpoint.
No real-model accuracy or availability claim follows from the offline suite.

Run the full offline regression from the project root:

```powershell
.venv/Scripts/python.exe -m pytest -q --basetemp tmp/pytest-memory-context-check
```

Use a fresh `--basetemp` path for each concurrent run. The suite also covers service
restart, multi-step context reuse, session isolation, filesystem failures,
summary cancellation and overflow recovery without duplicate tool calls.
