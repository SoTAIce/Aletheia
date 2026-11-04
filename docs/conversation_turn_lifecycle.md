# Conversation Turn lifecycle (2026-10-27)

## Ownership and identity

- RagAgentService owns accepted user interactions. Each accepted request has one Turn; an explicitly resumed task can have multiple Turns.
- ConversationMemory owns the recent view and lifecycle access. ConversationArchive owns durable originals and completion records.
- AgentRuntime owns task execution. Rebinding request-scoped models/registries preserves the existing WorkingMemory, execution/replan counters and successful step receipts. TaskState semantics are unchanged.
- The service creates Runtime without a conversation writer to avoid double recording. The optional standalone Runtime writer remains compatible, but only knows the TaskState goal/resolved answer; use the service to preserve exact inbound text and request identity.

## Publication and recovery

1. Validate identity and explicit resume mapping before changing a task.
2. Save RECEIVED with exact user text (including whitespace), ingress time, turn_id and task_id, before model/tool execution.
3. Publish one immutable completion sidecar with assistant reply, finished_at, execution result and COMPLETED/BLOCKED/FAILED/CANCELLED status. The original input JSON is never replaced.
4. Archive reads materialize original plus validated completion. Legacy v1 originals and existing summaries remain readable; new original records use v2.
5. Critical ingress/completion writes settle before cancellation releases the service session lock. Publication/reload errors propagate. An available in-process result can be published on retry without model/tool execution.

BLOCKED is the outcome of a finished interaction, not a finished task. Answering its question produces a new Turn referencing the same task_id and the resolved question_id. FAILED and CANCELLED remain visible history; cancellation does not imply tool side effects were rolled back.

## Calling the service

```python
first = await service.query_result(
    "Read the report and prepare a summary", "session-1",
    user_id="alice", request_id="message-1",
)
# If first.status is TaskStatus.BLOCKED, provide the requested answer:
resumed = await service.query_result(
    "Use report.txt", "session-1", user_id="alice",
    request_id="message-2", resume_task_id=first.task_id,
)
```

Keep request_id stable across retries of the same interaction. A new request_id means a new interaction, even if text is identical. Missing request_id preserves the old API's new-interaction behavior. query() and query_stream() forward the same optional identity/resume arguments.

If exactly one current-goal blocking question exists, it is selected automatically. Otherwise the caller must provide its explicit question_id. Wrong user/session/task/question is rejected before resolving a question or saving a new input. There is no text-based guessing of which task to resume.

Same request_id + original text + resume mapping returns the archived execution result, including after a service restart. Changed content/mapping is a conflict. An original with no completion is explicitly reported as incomplete; automatic uncertain execution replay is prohibited. A failed/cancelled interaction without an execution receipt reports its recorded status rather than running again.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/test_turn_lifecycle.py -q --basetemp tmp/pytest-turn-check-UNIQUE
.venv/Scripts/python.exe -m pytest -q --basetemp tmp/pytest-turn-full-UNIQUE
```

Final evidence: 26 lifecycle tests passed; focused archive/model/I/O/summary/runtime/service regression 120 passed, 1 skipped; full suite 942 passed, 2 skipped. Python compilation and scoped whitespace checks passed. Independent read-only review found no remaining Critical/Important issues, including the concurrent completion-time case.

Acceptance covers original input before model calls, cross-Turn resume after a real local tool call without replay, unchanged execution limits, user/session isolation, concurrent request deduplication, completion publication failure/retry, cancellation during ingress/initial/retry completion, failed setup/business execution, legacy recovery, corrupt outcome rejection and first-writer completion-time retention.

## Explicit boundaries

- Paused task execution resumes only within the same service instance/process. Restart restores history/results, not live WorkingMemory or external-tool checkpoints.
- Multiple processes are not coordinated. Different service instances do not share paused runtime objects. Archive publication locks coordinate instances in one process only.
- File storage must be application-owned; hostile concurrent filesystem mutation is outside the guarantee.
- Pure-memory mode has no restart durability. Its lifecycle receipts survive recent-cache eviction until clear()/process exit and consume memory proportional to accepted interactions.
- Missing completion after a crash may mean external work already happened. No exactly-once external side-effect or automatic rollback guarantee is made.
- CLI/UI request/resume wiring is unchanged. The new keywords are available on the service API.
- Summary selection, thresholds, background compression and performance algorithms were not changed. Existing source snapshots and history are retained. No commit/push or real API calls were made.
