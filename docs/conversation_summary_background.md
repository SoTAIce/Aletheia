# Conversation stage summaries and background compression

Implemented 2026-10-30: approved P0 summary correctness and P1 background scheduling. P2 automatic loading of summary bodies into ContextManager remains deferred; existing summary-index discovery and read_context_artifact are retained.

## Policy

- Archive-backed summaries are generated from raw turns, with previous_summary=None. Originals and outcome sidecars remain authoritative and recoverable.
- Only turns outside the recent cache window are eligible. RECEIVED is excluded. BLOCKED/FAILED/CANCELLED can be summarized as interaction outcomes, never as proof that their tasks succeeded.
- One batch contains one known Task, or a legacy unknown-task group. Turns from different tasks are not mixed. Default limits remain8 turns / estimated6000 content tokens per batch.
- SummaryEntry adds optional task_id. Existing four-field artifact metadata stays readable. Source IDs/task/statuses are bound by the program. Titles come from source input, descriptions combine recorded outcomes and a generated-text preview.
- LLM input includes task/question IDs, timestamps and lifecycle status. Six required headings cover goals/constraints, confirmed information, completed work, failures/cancellations, open questions and sources. Returned sections are format-checked, not fact-verified.
- LLMCompressor separately checks estimated full prompt input (default8192) and output cap (unchanged512). Counts are mixed-language estimates, not a provider tokenizer.

## Background flow

Service archive memories skip inline model compression in add(). After a durable outcome, a key-only scheduler notification is best effort. Loading a session also discovers reconstructible old work. Summary generation cannot change a persisted business outcome or request a Task replan.

ConversationMemory snapshots sources under its lock, generates without that lock, stages an invisible artifact, then validates revision/source identity/coverage before the small atomic commit marker. New turns may be written while generation waits. Concurrent snapshots from different memory instances cannot commit overlapping sources through this path.

The scheduler bounds active work to2 and pending keys to64. Same-session notifications coalesce. A rotating scan recovers queue-full notifications from loaded sessions. Default scan interval30s, max small-backlog wait300s, model/batch timeout30s, maximum2 attempts per source batch, retry delay1s. Waiting age and retry deadlines survive queue saturation.

Max wait is an eligibility threshold, not a hard completion deadline under load. The queue is bounded; loaded-session maps plus small retry/timing metadata scale with loaded sessions, as existing service session retention does.

The short existing synchronous commit marker remains in the event loop to preserve clear/cancel atomicity. Background compression removes model waiting from the request path; it does not claim zero synchronous filesystem latency.

## Enabling and lifetime

Service injection (reuse an existing compressor, caller owns its client):

```python
from app.core.dependencies import open_conversation_compressor
from app.services.rag_agent_service import RagAgentService

async with open_conversation_compressor() as compressor:
    async with RagAgentService(
        memory_directory="data/conversations",
        conversation_compressor=compressor,
    ) as service:
        result = await service.query_result("Your goal", "session-1")
```

The factory uses the existing local compressor's model/base URL. It owns a separate client with SDK retries disabled, leaving the scheduler in control of attempts. Existing global client/model settings are unchanged. Injected compressors can have their own transport retries; callers must account for those separately.

CLI opt-in:

```powershell
$env:CONVERSATION_STORAGE_DIR = 'data/conversations'
.venv/Scripts/python.exe -m app.agent 'Your goal' --compress-history
```

The flag requires archive storage and uses the existing local model. Without it, previous CLI behavior is retained. The CLI closes service background work before closing its owned summary client. No real CLI/model request was executed during this implementation.

For manual lifetime management, call await service.aclose(timeout_seconds=5). Closing stops admission/scans, permits queued immediately eligible work and current generation to settle within the deadline, then cancels outstanding work. Delayed retries do not bypass backoff to run during shutdown. Uncommitted staged artifacts stay invisible. Do not expect a short-lived CLI command to drain an arbitrarily large backlog in5s.

## Failure and recovery

- Summary failures/timeouts keep raw sources and do not publish coverage. Exhausted batches stop automatic retries in the current scheduler until the work identity changes, explicit force compression succeeds, or a fresh scheduler starts. If the oldest batch remains failed, it can prevent later batches in that session from advancing; this is logged, rather than silently declaring coverage or retrying forever.
- Restart re-derives work when a session is loaded from originals minus committed summary coverage. Unloaded sessions are not scanned globally.
- clear() invalidates in-flight work and suspends automatic rediscovery for that memory until explicit restore()/add(). It does not erase durable originals or pre-existing summaries.
- An oversized turn that cannot fit a batch remains in the token backlog and original retrieval path. It is logged as unselectable, never marked covered. V1 does not split oversized sources or recursively summarize summaries.
- Pure-memory mode retains its prior inline behavior. Background recovery requires an archive. No multi-process coordination, task checkpointing, topic clustering, vector retrieval or automatic long-term-memory promotion is introduced.
- Real semantic summary quality and local model context-window compatibility still require real-model evaluation. Structural tests cannot establish that all facts or negations are preserved.

## Verification evidence

- Baseline:942 passed,2 skipped. Original source snapshot:tmp/summary-background-baseline-20261030.
- P0:4 new RED/GREEN tests; scoped67 passed; full946 passed,2 skipped.
- Snapshot path:4 additional RED/GREEN tests; scoped47 passed.
- Assembly:scoped60 passed. A mid-implementation full run had only the intentionally failing new CLI lifetime test (954 passed,1 failed,2 skipped); CLI assembly was then implemented and verified.
- Independent review found queue-saturation age/backoff loss and notification exception propagation. Regression tests reproduced and fixed all findings; clear+periodic-scan resurrection was also reproduced and fixed.
- Final new policy/background/scheduler tests:17 passed, independently verified17 passed. Final full:959 passed,2 skipped in18.87s (tmp/pytest-summary-full-final1).
- Compilation and owned-source whitespace checks passed. No real API calls, commits/pushes, default-model or unrelated agent-core changes.

Repeat checks with fresh workspace basetemp paths:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_summary_policy.py tests/test_background_summaries.py tests/test_summary_scheduler.py -q --basetemp tmp/pytest-summary-check-UNIQUE
.venv/Scripts/python.exe -m pytest -q --basetemp tmp/pytest-summary-full-UNIQUE
```

## Follow-up: full Agent reliability validation

tests/agent/test_summary_lifecycle_e2e.py adds10 offline executions (five provider modes, two rounds each) through actual planning/runtime/local tools, blocked resume, summary artifact reading and durable replay. All10 passed7.41s; fresh full suite969 passed/2skip26.38s. No production changes. See docs/conversation_summary_agent_validation.md for exact coverage and distinction from real-model summary fidelity.
