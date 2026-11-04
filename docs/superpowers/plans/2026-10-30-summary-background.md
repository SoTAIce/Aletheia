# Summary Policy and Background Compression Implementation Plan

> Execute inline using executing-plans, TDD and a fresh requesting-code-review at completion. User explicitly approved the preceding design and implementation; do not ask for approval again. No commits/pushes or real model calls.

**Goal:** Source-linked, task-aware conversation stage summaries generated without delaying user-facing turns.

**Architecture:** Reuse ConversationMemory/Archive and LLMCompressor. Archive-backed service memories opt out of request-inline generation; a service-owned bounded asyncio scheduler runs snapshot/generate/validate/commit. WorkingMemory and TaskState are unchanged.

**Tech Stack:** Existing Python/asyncio/OpenAI-compatible client/pytest; no new dependencies.

**Spec:** User-approved proposal in preceding conversation: P0 summary correctness then P1 background scheduling; P2 context consumption is deferred, existing artifact reads retained.

## Constraints and rulings
- Preserve all dirty user work. Snapshot: tmp/summary-background-baseline-20261030. Work in place since prerequisite modules are untracked/uncommitted; no reset/stash/worktree migration.
- Only memory/archive/compressor, new scheduler, service/CLI assembly and their tests/docs are owned. Do not edit planner/runner/executor or long-term memory/default models.
- Archive v1 summaries remain readable. Legacy turns are an explicit unknown-task group. RECEIVED is excluded; terminal interaction outcomes, including BLOCKED, are eligible outside the recent window.
- Never summarize a summary in archive mode. Bind source IDs/task/status programmatically; generated facts remain unverified and raw sources recoverable.
- One active compression per memory; deep detached snapshot, generate without memory lock, stage then revision/source/coverage check before small synchronous commit marker. clear must reject stale results.
- Single process only; retain old pure-memory compression behavior. No external queue, topic clustering/merging, vectors or task checkpoints.
- Oversized unsplittable turns stay explicitly pending (token backlog but no selectable batch), retain raw reads; V1 does not split sources or mark them covered.
- Scheduler defaults: concurrency2, pending64, model timeout30s, max wait300s, scan30s, at most2 attempts per eligible batch with 1s retry delay. Include SDK retries in limits; default background client uses max_retries=0.
- Service async context/aclose drains with a finite deadline; completion notification failure never changes a persisted business result. Restart reconstructs from originals and committed summaries when session loads, not replaying tools.
- CLI explicitly opts into existing local compressor with --compress-history; unchanged default model names and old CLI behavior when flag absent. Long-lived service injection enables archive background automatically.

## Review focus
Unfinished sidecars; cross-task source mixing; cancellation while staging disk writes; clear while model runs; duplicate job notifications/queue overflow/client shutdown. Test each in tasks below.

## P0: Summary correctness
Files: conversation_memory.py, conversation_archive.py, memory_compression/llm_compressor.py; new tests/test_summary_policy.py.
- [x] Write/run RED: exclude RECEIVED, group same task, legacy compatibility, failed/blocked metadata in prompt, bounded input including prompt, programmatic title/description and task provenance.
- [x] Implement metadata-only selection reuse, optional summary task metadata with legacy decode, raw-backed prompt headings, input preflight. Preserve compressor return-string interface and strict finish checks.
- [x] GREEN scoped archive/summary/compressor/I/O/lifecycle tests and full regression.

## P1a: Snapshot compression
Files: conversation_memory.py, conversation_archive.py; tests/test_background_summaries.py.
- [x] RED: slow model doesn't block add/complete; simultaneous force calls coalesce; clear/cancel drops staged output; source/coverage checks reject stale/duplicate commit; failed generation retains backlog.
- [x] Implement archive background flag and shared snapshot path; explicit force_compress waits one batch; archive automatic path remains compatible outside background mode. Commit generation failures never mutate source history.
- [x] GREEN scoped then full regression.

## P1b: Scheduler and assembly
Files: new app/services/summary_scheduler.py; rag_agent_service.py; core/dependencies.py; agent/__main__.py. Tests/test_summary_scheduler.py and service background integration.
- [x] RED: completion returns while model blocked, bounded global concurrency/queue, same-session coalescing, threshold/age, finite retry, shutdown timeout, restart reconstruction and source/session isolation.
- [x] Implement key-only bounded scheduling with periodic scans of loaded sessions; explicit start/close lifetime and owned client lifetime. Notify after durable outcome (including failed/cancelled outcomes); session-load notifications also recover old work.
- [x] GREEN relevant tests and full suite; independent read-only review; fix findings and update harness/docs. Do not run real APIs or claim semantic quality from mocks.

## Completion evidence
- Baseline942 passed/2skip; P0 full946 passed/2skip. Snapshot scoped47 passed; assembly scoped60 passed.
- Mid-work full:954 passed/1failed/2skip (new CLI lifetime RED, subsequently fixed). Final full959 passed/2skip,18.87s;17 new tests independently rechecked.
- Review fixes: age and retry-not-before survive full queues; notifications cannot change business outcomes; periodic scan cannot revive cleared views. All have RED/GREEN regression evidence.
- Ruling: full-queue scheduling metadata (generation-scoped age/retry deadlines) persists independently of bounded pending admission, so queue pressure cannot restart waits/backoff. Metadata scales with loaded sessions.
- Ruling: clear suspends automatic rediscovery until explicit restore/add; it remains a view operation, not durable deletion.
- Documentation:docs/conversation_summary_background.md. P2 deferred as approved. No APIs/commits/pushes or unrelated agent/default-model changes.
