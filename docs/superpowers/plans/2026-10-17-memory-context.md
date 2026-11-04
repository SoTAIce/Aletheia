# Memory and Context Implementation Plan

> **For agentic workers:** Apply Superpowers TDD and systematic-debugging task by task, with read-only requesting-code-review after each stage. The user explicitly authorized immediate execution P0 then P1 and prohibited commits.

**Goal:** Recoverable session history, bounded model context, source-linked stage summaries and recoverable tool output without changing TaskState semantics.

**Architecture:** ConversationMemory owns session storage/cache; WorkingMemory remains the authoritative task aggregate. ContextManager produces model-visible copies only. File-backed, session-bound ConversationArchive stores turns, summaries and tool artifacts; the existing registry provides bounded reads. Existing model protocols and Plan?Execute?Replan remain.

**Tech Stack:** Python 3.11+, standard-library files/asyncio/dataclasses, existing OpenAI-compatible adapter, pytest.

**Spec:** User-approved architecture and execution requirements in this conversation, captured in Global Constraints and the stage requirements below.

## Global Constraints
- Preserve public call signatures; optional keyword dependencies may be added.
- Do not change TaskState state transitions, default model names, existing user work, or unrelated long-term-memory modules.
- No Git commits, new dependencies, real model API requests, vector retrieval or automatic topic merging.
- P0 must pass review and regression before P1 starts.
- Work in the current checkout because required modules are untracked/uncommitted; a source snapshot is retained in tmp/memory-context-baseline-20261017. Never reset/stash/clean this checkout.
- Single-process filesystem scope: no cross-process execution checkpoint or exactly-once tool execution claim.

## Review Focus
1. Failed disk writes and cancelled awaits must not evict original turns.
2. Session traversal, symlinks and foreign artifact references must fail closed.
3. Mandatory task constraints and tool call/result pairing survive projection.
4. Summary completion failure or stale revision cannot erase source history.
5. Overflow recovery cannot replay completed tools or silently expand deadlines.

## P0-1: Conversation persistence
Files: create app/services/conversation_archive.py and tests/test_conversation_archive.py; extend app/models/conversation_memory.py and tests/models/test_conversation_memory.py.
Interfaces: ConversationArchive(root, session_id), save_turn(turn), load_turns(limit=None), read_turn(turn_id); synchronous file work is called via to_thread. ConversationMemory accepts optional archive, async restore(), and keeps old in-memory behavior when omitted.
- [x] Write failing tests: exact message/time/role roundtrip, idempotent same turn, conflicting ID, session isolation, malformed records, failed write leaves cache unchanged, restore last N, concurrent add.
- [x] Run tests; observe missing behavior fail.
- [x] Implement atomic publication, validated session-bound references, bounded live cache, persistence before eviction and idempotent add.
- [x] Run related tests then full pytest with a unique workspace basetemp.
- [x] Independent read-only review; fix substantive findings with regression tests. No commit.

## P0-2: Unified ContextManager
Files: create app/services/context_manager.py and tests/test_context_manager.py; integrate app/agent/planner.py, runtime.py, tool_calling_step_runner.py, openai_compatible_tool_calling_model.py and app/services/rag_agent_service.py; add context settings.
Interfaces: ContextManager owns count_request(messages, tools), prepare(messages, tools) and conversation projection; optional injection preserves existing model.generate protocol. Count final provider-shaped messages and tools, reserve output and safety. Hard ContextBudgetError when protected content cannot fit.
- [x] Write failing tests for exact budget boundary, schema/system/output accounting, complete conversation selection, no WorkingMemory mutation, mandatory content rejection.
- [x] Observe failures, implement deterministic request projections, remove k=3/12000 history slicing, wire persistent service history and explicit output reserve.
- [x] Full regression and independent review. P0 gate must pass before P1.

## P1-1: Stage summaries and index
Files: extend conversation_archive.py, conversation_memory.py, memory_compression/llm_compressor.py; add tests/test_conversation_summaries.py and tests/test_llm_compressor.py.
Interfaces: immutable summary entry contains memory_id,title,description,storage_ref,source_turn_ids,session_id. Summary body persists; index is rebuilt from entry metadata. Bounded raw source batches use existing compressor; no recursive summarization requirement.
- [x] Write failing tests for source-linked persistence/reload, lexical selection/read, failed/truncated/refused summaries, stale clear, bounded pending memory and reconstruction.
- [x] Observe failures; implement raw-backed summaries, atomic metadata publication and version-checked commits; keep original turns.
- [x] Full regression and independent review.

## P1-2: Tool artifacts and overflow recovery
Files: extend context manager/archive, runner, model errors/adapter/planning adapter, tool loader; create app/tools/context_archive_tool.py and tests.
Interfaces: archive.save_text/read_text use opaque session-bound refs; read_context_artifact validates ref, offset and size. ModelContextOverflowError distinguishes provider overflow. Manager may compress tool bodies on copies, preserving status and call IDs; at most one model request retry per generation.
- [x] Write failing tests for oversized results, protected read and traversal, unchanged raw receipts, paired calls/results, schema accounting, overflow once/twice, cancellation and no duplicate tool invocation.
- [x] Observe failures; archive tool output before replacing body; retry model generation only; retain runtime deadlines and call limits.
- [x] Full regression, independent review, offline service restart/multistep integration. Document limitations and each stage result.

## Execution ledger
- Baseline: existing uncommitted changes preserved in tmp/memory-context-baseline-20261017; no commits.
- P0-2 RED: missing ContextManager and missing planner/runtime injection; GREEN scoped 107 adapter/core tests, then 103 runtime/planner/core tests.
- P0 combined first regression: 804 passed, 2 skipped.
- P0 review fixed: adopt manager in low-level Runtime planner; session IDs hash directory names without restricting original API.
- P0 follow-up regression: 20 failures, 791 passed, 1 skipped, all 20 caused by Windows MAX_PATH after long hashed paths; fix ongoing, not accepted as test environment skips.
- P0 review outstanding: Python 3.11 Windows reparse-point detection fallback.
- Ruling: persistence remains optional (memory_directory argument or CONVERSATION_STORAGE_DIR), preserving legacy disk-free API; CLI/service users can explicitly enable it. No model defaults changed.
- P0 Windows RED→GREEN: extended paths preserve deep-root atomic writes; Python 3.11 reparse flags reject junctions without Path.is_junction.
- P0 latest full regression: 813 passed, 2 skipped under a long Windows basetemp. One pre-existing skip plus symlink privilege skip.
- P0-1 files: conversation_archive.py, conversation_memory.py, archive/memory tests, persistent-service integration.
- P0-2 files: context_manager.py, Planner/Runtime/Runner/provider serialization, RagAgentService/config, context tests.

## Final stage results (2026-10-18)

All stages are complete; no Git commit was created. Original uncommitted work was
preserved against the source snapshot. No real model API calls or new dependencies.

| Stage | Delivered | Verification / review | Remaining limits |
| --- | --- | --- | --- |
| P0-1 | Atomic session-scoped full turns, restore, bounded recent cache, failure-safe eviction and dedup | Archive/memory/service tests; P0 full regression 813 passed, 2 skipped; independent review clear | Single-process filesystem, cache clearing is not archive deletion |
| P0-2 | Shared final-request budgeting and protected-state projection, planner/runtime/service injection | Budget boundaries, schema/system accounting and immutable state tests; independent review clear | Estimated token count, protected oversized requests fail explicitly |
| P1-1 | Raw-backed stage summaries, external bodies, six-field index, lexical/source recall | Summary cancellation/restore regression tests; independent review 52 passed, 1 skipped, no Critical/Important | Explicit compressor injection; lexical recall and summary fidelity are limited |
| P1-2 | Session-bound artifact reads, full raw receipts, progressive projection, one overflow retry without tool replay | Final independent scoped review 48 passed, no Critical/Important | No cross-process exactly-once tool guarantee; ordinary timeouts retain existing behavior |

Final full regression command:
`.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp tmp/pytest-memory-context-final-20261018`

Result: **852 passed, 2 skipped in 13.79s**. Skips: one pre-existing test and
Windows symlink privilege coverage. Windows deep-path/reparse fallback tests run.

Additional RED-to-GREEN review findings fixed:
- Windows MAX_PATH during atomic publish; extended filesystem paths.
- Stale summary publication after clear/cancel; draft + revision-checked commit marker.
- Oversized unsummarizable originals were invisible; bounded raw-source discovery/read.
- Restore captured revision too late; guard begins before first await.
- Tool observations lost call identity; preserve call_id/tool_name and full receipts.
- Nested ContextManagedModel could multiply retries; wrapper ownership flag.
- Overflow retry deadline caused Replan/tool replay; record interrupted recovery and terminate.
- RAG history packet omitted rewrite prompt/schema overhead; shared build_prompt and
  complete rewrite-request estimate, optional history reduction and explicit rejection.

Usage and limitations: docs/agent_runtime.md, Recoverable Memory and Context.
`.env.example` documents opt-in storage and budget configuration; default model
configuration is unchanged. data/conversations/ is ignored to protect local history.
