# Automatic summary bodies and live verification

Implemented2026-10-31. This completes P2 context consumption after the earlier archive/lifecycle/background stages. Default model names, .env, TaskState semantics, planner/executor/runner and long-term-memory code were not changed.

## Implementation

- ContextManager.prepare_conversation() discovers the existing relevant summary index and automatically loads eligible bodies through ConversationMemory.load_summary(). It supplies content, task/session/source IDs, title and storage_ref together, tagged historical_summary.
- Default body allocation is2048 estimated serialized tokens, capped at one quarter of window minus output/safety reserves. Up to3 index candidates are considered. Set summary_body_tokens=0 on ContextManager to disable body loading. Admission includes body wrappers; oversized bodies are omitted whole, with index references retained for normal artifact reads.
- ConversationMemory.find_summaries() adds CJK bigram matching for Chinese sentences, preserving Unicode word matching for other languages. This remains lexical selection, not semantic/vector retrieval or fact verification.
- Summaries are explicitly marked untrusted historical reference data. The current goal and constraints remain authoritative; projection never rewrites WorkingMemory or source archives.
- Sources fully present in recent raw Turns are not duplicated as loaded bodies. Unreadable/missing/corrupt bodies fall back to index references. Cancellation propagates, and clear() revision changes invalidate pending projections.
- Final provider-shaped request preparation still counts system messages, schemas, serialized messages, output reserve and safety. Older raw turns are reduced first; two recent turns take priority over optional bodies; the lowest-ranked whole body is then removed when needed. Aggressive overflow recovery removes optional bodies as well. No tool replay is introduced.
- Existing service/Runtime calls to prepare_conversation() carry the selected bodies into both Planner and execution context. This is a task-context snapshot, not a disk rescan at every model token/call; active task state remains stable while background summaries are generated.

Owned production changes: app/services/context_manager.py and app/models/conversation_memory.py. New tests:tests/test_summary_context.py. Opt-in live harness:scripts/validate_summary_context.py.

## Automated verification

13 new tests cover relevance/provenance, irrelevant exclusion, Chinese and other Unicode queries, wrapper/body budgets, final request reduction, overflow, protected goal/constraints, recent-source overlap, unreadable body fallback, clear during read, service Planner/Runner consumption without lookup, and validation isolation helpers.

Final full regression:982 passed,2 skipped in27.21s (tmp/pytest-summary-context-final2-full). Independent read-only review reported no remaining Critical/Important issues after fixes. Compilation and scoped whitespace checks passed.

## Real model setup and local limitation

Configured main model:qwen3.8-flash via existing DashScope settings. Default summary model remains local qwen3:4b-instruct.

Local inventory lists the model, but generation returns404. A native /api/show diagnostic returns500 because a referenced GGUF blob is missing. The model manifest exists without the required weight file. No weights were downloaded, no model cache was changed, and the production default remains unchanged.

The live harness therefore explicitly injected the configured remote model for summarization with --summary-provider remote. It used the same configured thinking setting as the main model. Ordinary sandbox connectivity failed; the bounded real requests ran with authorized network access. Only synthetic transcripts were sent; no user history or credentials were logged.

## Final controlled experiment

Raw final record:tmp/live-summary-context-20261031-1cabbc6d/report.json.

Three synthetic source sets, two rounds each:
- Database choice/port, SQLite prohibition, failed migration and undetermined owner.
- Revised replica target, no restart and cancelled rollout.
- Chinese documentation/source attribution, read-only access and pending approval.

Real LLMCompressor generated each summary from persisted raw turns; real archives committed/read it. Each enabled/disabled arm used an independent archive with neutral summary-index metadata and no answer-bearing raw previews. Both arms had an empty validation tool registry, preventing artifact lookup as an alternate source. Actual request capture verified positive/zero summary body counts respectively and zero tool calls in all12 runs.

| Measure | Bodies enabled | Bodies disabled |
|---|---:|---:|
| Agent task completed |6/6|5/6|
| Last deliverable step matches all predetermined fields |6/6|0/6|
| Final combined response is one correct JSON object |5/6|0/6|
| Any tool invocation |0/6|0/6|

The enabled 北辰 round0 result contains two JSON objects concatenated by the existing multi-step result aggregation. Its last-step answer is correct, but it is not a strict single-JSON final-response pass. The disabled 北辰 round1 planning failed the existing300-character step validation; that is a real baseline failure, not a budget/validation-rule relaxation.

One generated summary is English despite Chinese source material. Its PostgreSQL/6432, SQLite prohibition, failed migration and undetermined-owner semantics were manually checked against sources and preserved. It fails the original Chinese-only probes and the requested body-language convention; the raw failure flag is retained. The other5 summaries pass those probes. This report does not claim every real-model quality criterion passed.

This establishes body delivery and observed factual use in these six synthetic recall tasks. It is not a statistical reliability guarantee, a general recall benchmark or proof of freedom from hallucination. Source-linked summaries remain lossy generated data.

## Latency in this live sample

- Remote summary generation:2.08–4.31s per sample (background work, not a foreground wait in normal service use).
- Enabled Agent task:3.94–6.26s, median4.12s, including planning/execution model calls and local persistence.
- Context preparation alone:median23.43ms enabled vs18.09ms disabled (~5.34ms median difference). This is a small synthetic sample, not a production latency SLA.
- Non-streaming calls were measured; no time-to-first-token claim is made. Same-device local inference contention remains unverified because local weights are missing.

## Retained pilots and corrections

All previous artifacts remain:
- 824efc26:local summary generation404; no Agent phase.
- 921cfa22:remote pilot with a timeout, an artificial execution cap, and answer-bearing index previews; not body-only causal evidence.
- ebd0266d:neutral metadata, but prompt-only no-tool restriction was insufficient; actual models sometimes read artifacts. Also had a numeric probe false positive from timestamps.
- f0098b04:targeted numeric-gate rerun, still with tools available; not body-only evidence.
- 1cabbc6d:final no-tool, isolated controlled run reported above.

A Unicode selection regression, numeric leakage-gate false positives and tool-assisted control leakage were caught and corrected before final reporting. Original pilot outputs were not overwritten or relabeled as final passes.

## Re-running

```powershell
.venv/Scripts/python.exe -m pytest tests/test_summary_context.py -q --basetemp tmp/pytest-summary-context-UNIQUE
# Explicit real API calls on synthetic fixtures:
.venv/Scripts/python.exe scripts/validate_summary_context.py --live --rounds 2 --summary-provider remote
```

The harness requires --live. Local validation is available with --summary-provider local after the existing weight installation is repaired. It does not automatically replace production models, install dependencies or download weights. Agent models remain real in both arms; only archive tools are disabled for this controlled comparison.
