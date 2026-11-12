# Agent Runtime

Every task is one agent loop (D-076; plan mode and the plan/execute skeleton were removed in stage 4c):

```text
Goal -> RunMemory(RunState + Resources)
     -> RunAssembler: route (single / team / auto), read-only view, runner factory
     -> LoopRun: lock, budget, step deadline, side-effect watch, events and trace
     -> AgentLoopRunner (single) / ExploreLoopRunner (single with --delegate) / DelegatingLoopRunner (team)
     -> ToolRegistry -> local RAG / local tool / MCPToolAdapter -> MCPClient
     -> LoopRun commits: completed answer / blocked question / failed with a failure kind
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
  becomes a failed tool result.
- `RunAssembler` (`app/services/run_assembler.py`) chooses the mode once per task
  (kept when a blocked task resumes), withdraws write and run tools from read-only
  tasks, picks the runner factory and builds the task's `LoopRun`; a resumed task
  is rebound to the request's fresh model and tools instead.
- `LoopRun` (`app/agent/loop_run.py`) owns the host guarantees: one run at a time
  per task, rebind invalidation (a late result or failure of an old binding is
  stale), no commit after cancellation was requested, the state-version check, the
  run budget, the step deadline (the time spent waiting for a user's approval is
  not counted, D-061), the side-effect watch, the execution limit and the failure
  kinds. It reports the task's one step as `plan` and `step` events and trace spans.
  Runners only return output; `LoopRun` validates it (content trimmed, source
  references must be registered resources) and commits it to `RunState`.
- `AgentLoopRunner` (`app/agent/agent_loop.py`) calls tools until the model
  replies with plain text, which is the final answer (no JSON envelope). Two
  control tools never reach the registry: `todo` records the model's own
  checklist, `ask_user` blocks the task with one question. The host repairs a
  bounded number of mistakes (two each): undecodable tool arguments, calls to
  tools that are not offered, and empty replies. When the round budget is spent
  the model is asked once more for its answer with the tools shown but not
  callable; asking for tools again then fails the step.
- The team (`DelegatingLoopRunner`, D-075) is the same loop for the lead, with host
  tools `delegate` (and `review`) instead of workspace tools; each delegation runs
  a fresh member loop. `--delegate` lets the single loop hand wide read-only
  research to a member (`ExploreLoopRunner`, off by default).

## Outcomes and limits

| What happens | Task result |
| --- | --- |
| plain-text reply | `COMPLETED`, content is the trimmed answer |
| `ask_user` | `BLOCKED`, content is the question; a blocking question is recorded |
| round limit, a refused or failing read, a timeout after reads only, unavailable tools after the repairs | `FAILED`, `limit` |
| a write or run tool raised, or a timeout after a write or run | `FAILED`, `side_effect` (never replayed) |
| repeated empty replies | `FAILED`, `format` |
| provider error or request timeout | `FAILED`, `model` |
| a gate refused a model or tool call | `FAILED`, `policy` |
| the team's own time budget, or overflow recovery interrupted | `FAILED`, `timeout` |
| token or time budget spent | `FAILED`, `budget` |
| runner contract errors and other unexpected exceptions | `FAILED`, `internal` |
| cancellation | the task is failed and `CancelledError` propagates; the registry context closes tools |

Every failure kind has a Chinese explanation in `FAILURE_MESSAGES`
(`AgentRunResult.user_message`); `content` keeps the English diagnostic. A loop
never replays a failed task: there is no replanning (`replan_count` stays 0).

Defaults: 16 executions per task (counted across resumes); the loop's own limits
are 20 tool rounds, 40 tool calls and 480 seconds (`LoopOptions`); the step
deadline is the larger of `AGENT_STEP_TIMEOUT_SECONDS` (default 60) and the
runner's limit plus 5 seconds, and a caller's run budget can only shorten it. The
token budget is checked before and after the loop's step, not between the model
calls inside it, so one step can finish its calls and then fail the task on
`budget` (the step's receipt is kept).

`RAG_JSON_MODE=true` requests provider JSON-object output. `RAG_ENABLE_THINKING` is
optional and provider specific. Unsupported provider options surface as errors,
without an automatic unstructured fallback.

## Memory and blocked-task resume

`RunState` is the task's authoritative state; ConversationMemory remains session
scoped. The loop sees the recent conversation, long-term memories and resource
excerpts as pinned context, never as instructions.

`RagAgentService` runs each query as a new task and retains ConversationMemory by
session. Queries for the same session are serialized. A blocked task stays alive
in the same service instance: answer it with `resume_task_id` (and `question_id`
when more than one question is open). The resumed loop continues its own messages
with the answer as the result of its `ask_user` call; after a restart only the
archived history remains, and a new task starts with the answer in its context.

At the lowest level the same steps look like this:

```python
from app.agent.agent_loop import AgentLoopRunner, LOOP_STEP, LoopResumeStore
from app.agent.loop_run import LoopRun
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.tools.tool_loader import open_tool_registry

async def execute_goal(goal, model, servers, get_answer):
    memory = RunMemory(RunState("user", goal))
    store = LoopResumeStore()
    async with open_tool_registry(memory, mcp_servers=servers) as registry:
        run = LoopRun(memory, model, registry, step_description=LOOP_STEP, runner_factory=lambda deps:
                      AgentLoopRunner(deps.model, deps.tool_registry, None, deps.context_manager, store))
        result = await run.run()
        if result.status is TaskStatus.BLOCKED:
            for question in memory.task_state.scratchpad.open_questions:
                if question.blocking and question.resolved_at is None:
                    memory.task_state.resolve_question(question.question_id, await get_answer(question.content))
            result = await run.run()
        return result
```

From the command line, the interactive mode does this for you (D-040): run
`.venv/Scripts/python.exe -m app.agent --no-knowledge` with no goal (or with
`-i`). One service and session stay open across turns, so later goals see the
earlier conversation; when a task blocks, the next line is its answer. `/new`
drops the waiting task, `/help` shows the commands, `/exit` or end of input
leaves, and Ctrl+C stops the program (the interrupted turn is recorded as
cancelled). `--max-tokens` and `--max-seconds` apply to each turn separately;
`--result-json` needs a single goal. A blocked task lives only in that process: a
restart restores the archived history, not the waiting task.

## Current limits

The service reads `RAG_MODEL_TIMEOUT_SECONDS` (default 30) and
`AGENT_STEP_TIMEOUT_SECONDS` (default 60). `AGENT_PLANNING_TIMEOUT_SECONDS` is
still accepted in `.env` but no longer read (plan mode was removed). For slower
real API responses, increase these finite limits rather than disabling them; a
request timeout is a failed execution, never a completed one.

- This is sequential, process-local execution. No task checkpoint persistence,
  cross-process recovery or transactional rollback of external tool side effects
  is implemented. Timeout/cancellation may occur after a side effect has happened.
- The default service uses stdio Filesystem/WebSearch MCP and local RAG/time
  tools. Historical HTTP CLS/monitor configuration is not wired into this
  runtime; the unused `aiops` experiment modules were removed on 2026-11-08
  (cleanup-stage0-001). Legacy tool exports remain available for compatibility.
- `query_stream()` retains an event interface but emits final content/status,
  not per-token output. `query_result()` is the status-aware API.
- The model's plain-text reply is taken as the answer; this is not an independent
  semantic verifier. The team's completion is decided by the host (D-075).
- Tests use real stdio MCP subprocesses and the real provider-message adapter,
  with deterministic model, RAG and Tavily responses. Live validation separately
  requires working provider credentials, Milvus and the local reranker. Passing
  the automated suite does not establish provider availability or long-term
  stability.


## Long-term memory (D-042)

Off by default. Set `LONGTERM_MEMORY_DSN` (PostgreSQL) to turn it on; vectors go
to `MILVUS_HOST`/`MILVUS_PORT` in `LONGTERM_MEMORY_COLLECTION` (default
`agent_memories`, never the knowledge base collection). The CLI then opens the
store, index and model client through `open_longterm_memory()` and closes them on
exit. Memories are scoped by `--user-id` (default `local`).

- Recall: before a new task runs, up to `LONGTERM_RECALL_LIMIT` memories are
  recalled for the goal and pinned as `longterm_memory` within
  `LONGTERM_CONTEXT_TOKENS` (default 600), with a policy saying they are reference
  data, not instructions. Recall failures or a 3 s timeout only log a warning.
- Writes are explicit only: in the interactive mode `/remember text` stores the
  text as given (no model call), `/memories` lists them and `/forget ID` deletes
  one. Nothing is extracted automatically from conversations.

## Recoverable Memory and Context (P0/P1)

Conversation persistence is on by default: `CONVERSATION_STORAGE_DIR` defaults
to `data/conversations`, and a relative value resolves from the project root, not
the working directory. Set `CONVERSATION_STORAGE_DIR=` (empty), or pass
`RagAgentService(memory_directory="")`, to keep the in-memory mode. The test suite
(`tests/conftest.py`) and the bench (`bench/run_bench.py`) turn the default archive
off; tests that need one pass `tmp_path`. The application must own this directory and
supply the authenticated session ID; a session ID is a storage scope, not an
authentication mechanism. Custom archive directories must also be excluded from Git.

The service restores the same session's recent conversation cache when first
accessed. `ConversationMemory` retains 20 complete turns by default, while all
original turns remain in the archive. Immutable turn files are published atomically
before cache eviction. Repeating the same turn ID and contents is idempotent;
conflicting contents fail explicitly. Read/write failures propagate rather than
silently accepting lost history. `clear`, `pop` and `remove` affect the in-memory
view, not the durable archive; this is not a permanent deletion API.

`RunState` remains the authoritative task state. `ContextManager` projects
copies for the loop's model requests and never edits that state. Configure
`CONTEXT_WINDOW_TOKENS`, `CONTEXT_OUTPUT_TOKENS` and `CONTEXT_SAFETY_TOKENS`
(defaults 32768/8192/1024; the output cap was 2048 before D-036) for the deployed model. Estimates include serialized
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
