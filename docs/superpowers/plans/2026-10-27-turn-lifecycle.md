# Conversation Turn Lifecycle Implementation Plan

Goal: preserve exact input/time, associate turns with tasks, resume blocked tasks across turns, preserve failures/cancellation, deduplicate requests and keep legacy archive restoration.

User authorizes implementation; no commits/pushes or real model calls. Existing dirty files are snapshotted in tmp/turn-lifecycle-baseline-20261027 and must not be reset. Summary strategies, background compression, performance algorithms and TaskState semantics are out of scope.

## Design rulings
- Service owns user-facing interaction lifecycle. Runtime returns task results; its optional standalone conversation integration remains compatible but uses explicit lifecycle records, never rewritten goals after execution.
- Add optional task_id/status/finished_at to ConversationTurn after existing positional fields; legacy turns retain their existing meaning and v1 encoding.
- New service turn begins RECEIVED with exact user text and ingress timestamp. A single immutable outcome sidecar adds assistant reply plus COMPLETED/BLOCKED/FAILED/CANCELLED and completion time. Original input file is never overwritten.
- Archive reads materialize original plus validated outcome. Metadata fingerprint includes outcome changes; load/recent/summary consumers still use existing interfaces.
- Sidecar may hold JSON-safe execution result for durable replay. Original save is idempotent against the original, not the materialized completion; conflicting original IDs or outcome writes fail.
- request_id is explicit, optional; same ID+input+resume target replays existing result without model/tools; different input/target conflicts. Missing ID means a new interaction. After crash with only RECEIVED, return an explicit incomplete-turn error, never rerun uncertain work automatically.
- Service retains blocked Runtime in-process, rebinds fresh model/tool sessions when resumed; preserves counters, valid results and task identity. Do not keep MCP contexts alive across request asyncio tasks. No cross-process task checkpoint/automatic resume claim.
- Resume uses resume_task_id plus question_id; if exactly one unresolved question, it can be selected. User text becomes that question's answer; validate ownership/session/task before mutation. New unrelated requests remain new tasks.
- Critical ingress/outcome I/O is awaited through cancellation cleanup so session lock is not released while a background commit is still unaccounted for. If outcome persistence fails, propagate failure; retained input/result permit safe replay without tools. Do not claim exactly-once external side effects.

## Stage 1: Archive/model lifecycle
- [x] Write failing tests for v1/v2 roundtrip, received input, immutable completion, idempotent/conflicting outcomes, malformed/foreign sidecar, mutation isolation, restart replay and publication failure.
- [x] Implement minimal schema and atomic sidecar write/read. Add Memory.complete_turn/read_turn/turn_result access using existing lock/worker/revision patterns; do not alter summary selection/thresholds.
- [x] Run archive/model/summary/I/O tests; verify old originals remain byte-identical and old snapshot metadata rules still work.

## Stage 2: Service + Runtime lifecycle
- [x] Tests first: input durable before first model call, original whitespace/time preserved, failure/cancel/early MCP/model setup error, request replay and conflict, no duplicate input on final-save failure.
- [x] Service accepts request_id/resume_task_id/question_id optional keywords, forwards user identity consistently; scoped locks use the same key as conversations. Fix existing missing math import and optional user_id default because this service is now in scope.
- [x] Runtime standalone integration begins before execution, completes once, records cancel/failure; service-managed runtime receives no conversation writer. Add safe rebind only when idle to refresh model/registry without resetting counters.
- [x] Do not reuse closed resources held by paused runtimes: rebind fresh clients before the subsequent run. Explicit resume resolves only the chosen question and preserves the active goal.

## Stage 3: Recovery/regression
- [x] Paused task with earlier successful tool work resumes in another Turn without replaying that work. Wrong session/user cannot resume; repeat blocked request cannot resolve twice.
- [x] Reopen archive after failed/cancelled/interrupted turn; replay completed request after restart from stored result; received-only duplicate is rejected, not executed.
- [x] Cancellation during ingress/final/retry disk write, write exceptions and retry, concurrent same request ID, unknown/multiple question mapping.
- [x] Run focused and full suite, independent read-only final review; update docs/harness. No performance or summary rework.

## Acceptance
Raw input persists before external work. Turn status is metadata, not a bracket prefix in assistant text. Each accepted request has one original and at most one completion; exact request retries reuse IDs/result. Blocked resume uses same task and new turn, retaining limits/results. Legacy v1 originals/summaries remain readable. History persistence does not imply process-restart task execution recovery.

## Final evidence
- Lifecycle RED/GREEN: initially missing TurnStatus, then missing service ingress API and standalone recording; added failure probes for mutable memory-only receipts and concurrent timestamp conflict, all subsequently green.
- Final focused command: tests/test_turn_lifecycle.py + archive/I/O/summaries/models + runtime/service/persistent-service: 120 passed, 1 skipped (tmp/pytest-turn-final-focused).
- Full regression: 942 passed, 2 skipped in 17.14s (tmp/pytest-turn-final-full). Existing service math/default-user/forwarding failures fixed within owned service scope.
- Independent read-only review: initial cancellation/retry/version/cleanup findings fixed and tested; final 26 lifecycle tests passed, no Critical/Important findings.
- Compilation and owned-source git diff --check passed. No model/API execution, commits/pushes or summary/performance changes.
- Details/limitations: docs/conversation_turn_lifecycle.md.
