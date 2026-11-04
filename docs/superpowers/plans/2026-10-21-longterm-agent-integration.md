# Long-term Memory Agent Integration Implementation Plan

> **For agentic workers:** Use Superpowers executing-plans and test-driven-development task by task, with requesting-code-review before completion. This is a proposal, not an instruction to implement or commit yet.

**Goal:** Let Planner and tool execution use the same user's cross-session long-term memories, then expose explicit application-controlled writes without automatically memorizing model output.

**Architecture:** Inject the existing LongTermMemoryService into RagAgentService. Recall once before planning, project source-linked optional memory context with ContextManager, and attach it through existing TaskState.pin_content. Runtime and TaskState lifecycle stay unchanged. Controlled writes use a separate application method, never a model-callable write tool.

**Tech Stack:** Existing Python/asyncio, PostgreSQL MemoryStore, Milvus MemoryVectorIndex, pytest; no new frameworks or dependencies.

**Spec:** User's conversation requirements: separate ConversationMemory, WorkingMemory and Long-term Memory; prioritize Agent integration before extraction-quality or retrieval redesign. Concrete defaults below are proposed implementation decisions, not claims of existing behavior.

## Global Constraints

- Plan only in this turn. Do not change business code, call live model APIs, or create/push commits without a subsequent instruction.
- Preserve legacy query/question/session positional arguments; new identity/dependencies are keyword-only.
- user_id is supplied by the trusted application, never inferred by the LLM and never silently replaced with session_id.
- No project isolation claim: records currently lack project_id. Do not infer project authorization from category=PROJECT or source_id.
- No automatic extraction from final replies, intermediate results, tool output, or all ConversationTurns.
- No new memory Agent, global memory singleton, schema migration, RRF changes, quality-review model, or autonomous write/delete tools.
- PostgreSQL remains authoritative. Preserve MemoryIndexSyncError; no automatic replay of remember(), rollback or distributed-transaction guarantee.
- Keep existing TaskState, AgentRunResult and Plan–Execute–Replan semantics.
- Tests remain local unless separately authorized for upload. Preserve existing dirty files, especially currently untracked memory_service.py.

## Review Focus

1. Equal session strings under different user IDs must not share conversation cache, lock scope or disk archive.
2. Missing identity with memory enabled must fail before database/model calls; disabled legacy calls must still work.
3. Huge, outdated or instruction-like memories must not become mandatory context or override current instructions.
4. Recall timeout/outage must be observable without failing an otherwise executable task; cancellation must still propagate.
5. Model retry, Replan and write/index errors must not trigger repeated extraction or database mutation.

## Source findings

- RagAgentService currently has only question/session_id; its cache/locks and ConversationArchive are keyed by session alone.
- It constructs WorkingMemory before Runtime.run(), so optional long-term context can be pinned there without changing Runtime.
- Both Planner and step execution already receive pinned_contexts through existing snapshots.
- ContextManager currently drops optional conversation history and archived tool previews, but has no long-term-memory reduction rule.
- LongTermMemoryService exposes recall(user_id, query, limit), remember(user_id,text,source,source_id), forget(user_id,memory_id).
- The service borrows initialized dependencies. Reads require PostgreSQL, Milvus and embeddings; writes additionally use extractor/resolver model clients.
- core/milvus_client.py patches MilvusClient to the default ORM alias globally. Creating another client does not automatically provide connection isolation.

## Task 1: Identity and dependency boundary

**Files:** Modify app/services/rag_agent_service.py. Create tests/agent/test_longterm_service_integration.py; extend tests/agent/test_persistent_service.py.

**Interfaces:**
- Add optional constructor keywords longterm_memory: LongTermMemoryService | None = None, longterm_recall_limit: int = 5, longterm_timeout_seconds: float = 3.0.
- Extend query_result(question, session_id, *, user_id: str | None = None), query(...) and query_stream(...) consistently.
- If longterm_memory is enabled, require explicit nonblank user_id (maximum 256 UTF-8 bytes). Validate limit 1..100 and finite positive timeout before I/O.
- Legacy calls without user_id and without long-term memory retain session-only behavior and existing storage names.
- Calls with user_id use tagged identity keys ("user", user_id, session_id), distinct from legacy keys, for cache/locks. Use a canonical JSON encoding of the tagged key as the archive session label; ConversationArchive already hashes labels. Do not build filesystem paths from IDs.

- [ ] Write tests named test_enabled_memory_requires_user_before_io, test_disabled_legacy_api_unchanged, test_same_session_different_users_isolated, test_user_history_restores_without_legacy_collision, test_query_and_stream_forward_identity. Assert no model/database calls on missing identity and no foreign turns after restart.
- [ ] Run `.venv/Scripts/python.exe -m pytest tests/agent/test_longterm_service_integration.py tests/agent/test_persistent_service.py -q --basetemp tmp/pytest-lt-agent-t1-red`; observe the missing interface/behavior failures.
- [ ] Implement only injection, validation and identity scoping. No recall yet; no automatic resource initialization or closure in RagAgentService.
- [ ] Repeat with a fresh green basetemp; existing session-only service tests must pass. Review identity and archive compatibility before Task 2.

## Task 2: Bounded, data-only memory projection

**Files:** Modify app/services/context_manager.py, app/agent/planner.py, app/agent/tool_calling_step_runner.py. Extend tests/test_context_manager.py and tests/agent/test_context_integration.py.

**Interfaces:**
- Add ContextManager.longterm_context(hits: list[MemoryHit], *, user_id: str, token_budget: int = 1200) -> str. Return JSON {"entries": [...]} suitable for source="longterm_memory"; empty input/budget overflow may return an empty entries array.
- Each entry contains memory_id, version, category, content, source, source_id, updated_at, expires_at. Do not expose the RRF score as confidence.
- Retain input ranking, discard mismatched users, duplicates and expired records. Invalid expiry/time types fail explicitly; reject malformed records rather than guessing dates.
- Count fully serialized JSON using the manager's counter. Remove whole lowest-ranked entries until within 1200 estimated tokens; never truncate a fact mid-sentence. Skip an individually oversized record so later short records can still fit.
- Extend final prepare(): archived tool previews first, lowest-ranked long-term entries next, optional conversation history next. Aggressive overflow recovery may remove all long-term entries. Required goal/constraints/step/state are unchanged.
- Planner and Runner trusted instructions explicitly identify longterm_memory as historical reference data; current explicit instructions win and embedded commands are not executable instructions. This is a prompt boundary, not a guarantee of semantic truth.

- [ ] Write test_longterm_projection_keeps_sources_and_excludes_score, test_longterm_scope_expiry_and_duplicates, test_oversized_memory_does_not_hide_shorter_hit, test_budget_drops_whole_memory_before_protected_state, test_aggressive_retry_can_drop_memories, test_projection_does_not_mutate_hits_or_working_memory.
- [ ] Run focused tests with a fresh workspace basetemp; verify RED before implementing the new projection/reduction behavior.
- [ ] Implement the projection and integrate its reduction into existing ContextManager. No database calls inside ContextManager; no memory content added as system instructions.
- [ ] Re-run context/Planner/Runner tests. Review serialization, source metadata and protected-state retention before Task 3.

## Task 3: Recall before planning, visible to the whole task

**Files:** Modify app/services/rag_agent_service.py. Extend tests/agent/test_longterm_service_integration.py.

**Interfaces / sequence:**
1. Validate identity; obtain scoped ConversationMemory and create existing ContextManager/WorkingMemory.
2. If dependency enabled, call await longterm_memory.recall(user_id=user_id, query=question, limit=5) within the configured 3.0-second timeout.
3. Build the Task 2 projection and pin nonempty entries using task.pin_content(payload, "longterm_memory") before runtime.run().
4. Run the unchanged Runtime; recall once per query, not once per step, repair request or Replan.
- Recall/formatting exceptions: log a bounded warning with exception type, no DSN/credentials/query/memory body; omit long-term context and continue. Do not catch BaseException/CancelledError.
- No additional model request is needed for memory selection. Empty results produce no memory pin. This phase adds no recall tool; user-driven dynamic lookup can be evaluated later.

- [ ] Write test_cross_session_memory_reaches_planner_and_runner using deterministic model responses and mocked memory dependency; source seed is not in the new session's conversation history.
- [ ] Write test_memory_called_once_across_steps_replan_and_format_retry, test_recall_failure_and_timeout_continue_without_memory, test_cancel_recall_propagates, test_current_instruction_overrides_historical_preference. For the last test assert trusted instruction placement; semantic compliance belongs to live validation.
- [ ] Observe RED, implement recall/pin, then run all service/runtime/context regression tests with a unique basetemp.
- [ ] Gate: two sessions for one user receive the same seeded memory, another user does not; task status and existing execution limits remain unchanged.

## Task 4: Explicit application-controlled writes

**Files:** Modify app/services/rag_agent_service.py. Extend tests/agent/test_longterm_service_integration.py.

**Interface:** async remember_message(self, text: str, *, user_id: str, source_id: str) -> list[MemoryDecision].
- Require enabled dependency and nonempty user_id/source_id/text before I/O; caller must supply the actual user-message identifier (or explicitly identified manual input).
- Delegate once to longterm_memory.remember(user_id, text, source="user_message", source_id=source_id). Do not concatenate assistant messages or task output.
- This is an application API called only after the application decides to save; not a ToolRegistry entry and not automatically called by query_result(). It does not add authentication of its own.
- Return all decisions, including CONFLICT. Propagate extraction/store errors and MemoryIndexSyncError unchanged; preserve metadata for repair and never automatically retry the whole operation.
- No write timeout layered here: injected model/database dependencies retain their timeouts. Cancellation retains existing uncertain-write semantics.

- [ ] Write test_explicit_write_delegates_user_message_only, test_query_never_automatically_writes, test_write_requires_enabled_dependency_and_provenance, test_write_conflict_is_returned, test_partial_index_failure_not_replayed.
- [ ] Observe RED, implement the small delegation method, and verify GREEN. Existing memory_service.py is reused, not redesigned.
- [ ] Review that retry/repair/Replan cannot reach this write entry and that automatic forget/update tools were not introduced.

## Task 5: Resource wiring, runnable verification and documentation

**Files:** Modify docs/agent_runtime.md. Create scripts/live_longterm_agent_smoke.py and tests/agent/test_longterm_live_smoke.py (mocked resources only). No production default-model changes.

**Composition:**
- The smoke program owns PostgresMemoryStore, AsyncOpenAI and embeddings client lifetimes; initialize store/index once, inject LongTermMemoryService, close after all Agent requests finish, including setup failure paths.
- Borrow the application's shared MilvusClient through the existing manager, with independent collection agent_memories. Never disconnect the default alias or close the borrowed client inside MemoryVectorIndex/RagAgentService. The process owner may close the manager only after every RAG/memory consumer has stopped. Do not claim a second client is isolated under the current global alias patch.
- Use existing DashScopeEmbeddings/configuration. Model selection uses existing config.rag_model unless an explicit CLI override is supplied. Read PostgreSQL DSN from an environment variable, never CLI/log output.
- Script flags: --user-id (required), --session-a, --session-b, --seed-text, --run-id (required provenance). Use memory_directory for scoped history; disable unrelated MCP/RAG tools in this focused smoke.
- Script explicitly invokes remember_message for the supplied seed once, reports decision types, recalls directly, then starts a different session and captures actual Planner/Runner memory packets. It must not assert success solely because the final answer repeats seed text.
- Repeat verification in a restarted process without reseeding; a different user must not receive the seed memory. Test current instruction conflict and a read outage separately. Do not automatically delete/update existing user memories during smoke; use dedicated test users and explicit cleanup outside the script.

- [ ] Write mocked resource-lifetime tests (setup failure, normal completion, cancellation; request code never closes the shared client). Observe RED, implement the smoke wiring and verify GREEN.
- [ ] Document optional injection, explicit identity, legacy session compatibility, manual writes, read outage policy, 1200-token projection cap and remaining limitations.
- [ ] Run `.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp tmp/pytest-lt-agent-final-UNIQUE`; require no new failures. Request read-only final code review.
- [ ] Only when separately authorized, run live smoke with configured PostgreSQL/Milvus/model. Report actual calls, decisions, saved IDs, cross-session source visibility and failure outcomes without exposing credentials or full private memory bodies.

## Acceptance and stop conditions

- First deliverable is Tasks 1–3: read-only cross-session use with no writes and unchanged legacy behavior. Stop expansion if identity isolation or protected-context budgeting fails.
- Second deliverable is Task 4: explicit save then a different-session query uses it; partial failure is visible and never replays writes.
- Offline tests validate integration contracts, not extraction quality or semantic instruction-following. Live results must be separately reported, never inferred from mocks.
- Project scoping, recall quality improvements, memory aging, autonomous writes/deletes, and durable retry/outbox mechanisms are deferred. Current reads may return irrelevant top-K; do not invent an uncalibrated confidence cutoff.
- RRF is unchanged. Index outages currently affect the existing combined retriever; this plan's read fallback is to omit memory, not to redesign retrieval.

## Self-review

- Each new interface is defined before use; question/session legacy signatures and memory return types stay compatible.
- Identity-scoped history is included because scoped recall alone would still leak shared session history.
- Budget reduction is included because ordinary pinned_content is otherwise protected and could break task execution.
- Resource sharing accounts for the actual global Milvus alias patch. No per-request disconnect or automatic reconstruction of live dependencies.
- Proposal adds one optional dependency, identity propagation, one context projection and one explicit write method. It leaves Runtime/TaskState and the existing long-term pipeline intact.
