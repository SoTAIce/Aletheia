# Summary Body Context and Real-Model Validation Implementation Plan

> Execute inline with writing-plans/TDD/executing-plans and final read-only code review. User explicitly authorizes implementation and real-model validation; no additional approval round. No commits/pushes/default-model changes.

**Goal:** Relevant archived summary bodies reach Planner and execution models automatically within bounded optional context, then validate on configured real providers.
**Architecture:** Reuse memory summary index/read access and ContextManager. Load at most3 relevant bodies (default2048 estimated serialized tokens, capped by available input budget), preserve references/provenance, and remove whole low-priority bodies during final exact request projection. WorkingMemory/TaskState original state is unchanged.
**Tech Stack:** Existing Python/asyncio/pytest/OpenAI-compatible clients. No dependencies added.
**Spec:** User-approved P2 automatic summary-body context plus real-model verification, after completed summary/background work.

## Scope and rulings
- Dirty work stays intact; snapshot tmp/summary-context-baseline-20261031; no worktree/reset/stash because prerequisite modules remain uncommitted.
- Own ContextManager, minimal summary lexical selection in ConversationMemory, tests, validation scripts/docs/harness only. Preserve unrelated longterm/agent-core edits.
- Keep summary metadata discovery/permission-controlled raw/artifact reads. No vector retrieval, topic merge, new database or summary recursion.
- Chinese lexical matching needs bounded CJK bigrams rather than whole-sentence exact terms; this is lexical relevance, not verified semantics.
- Summary bodies are optional historical data, not authoritative instructions/current task state. Current goals/constraints remain protected. Skip sources fully present in recent turns; keep references for bodies omitted by budget.
- Final prepare() includes schemas/system/output/safety and can remove entire lowest-priority bodies before other history; overflow retry removes optional bodies too. Admission counts serialized wrapper and content, never truncates source text to claim it fits.
- Unreadable/corrupt body degrades to its index reference with warning; cancellation propagates. Revision checks prevent clear() resurrecting a pending projection.
- Real-model inputs are synthetic fixed facts/negations/failure+blocked cases. Store outputs, timings and pass/fail checks; finite calls/timeouts, no API-key logging or .env changes.
- Current configured main model is qwen3.8-flash. Probe existing local compressor availability; if absent validate using explicit temporary injection of configured remote model and report that local compressor is unverified. No automatic model installation/fallback in production.
- Live truth checks use predetermined required/forbidden facts, not LLM self-confidence. Distinguish limited empirical pass from proof of summary fidelity.

## Stage1: P2 TDD
- [x] RED tests for relevant body auto-load/provenance, irrelevant exclusion, Chinese queries, wrapper budget, final budget/drop order/overflow, raw overlap, corrupt body, clear during read, service Planner+Runner consumption without artifact tools.
- [x] Implement minimal optional summary-body projection and CJK lexical improvement; keep public API compatibility via keyword defaults.
- [x] Scoped + full regression, source checks and independent read-only review.

## Stage2: Real validation
- [x] Probe model configuration/availability without printing secrets. Local manifest exists but GGUF blob missing; generation404/show500. No downloads/default changes.
- [x] Generate summaries from fixed synthetic source sets, commit/read them through real archives; repeat evaluation of required facts/negations and uncertainty. Remote injected explicitly for6 samples; one English body fails language/Chinese-probe criterion.
- [x] Run configured real Planner/Runtime/Runner on a synthetic recall goal whose answer is only in loaded old summary bodies; check known answer, completion status, source projection and model-call latency. Final A/B independent/neutral archives and empty tool registry; all12 runs have zero tool calls.
- [x] Measure extra context preparation latency; report model latency separately. No unsupported TTFT claim from non-streaming calls.
- [x] Save reproducible validation scripts/raw artifacts and precise limitations; update harness. Any unreachable provider/quota error is reported rather than claiming live validation passed.

## Evidence and rulings
- Initial7 RED tests reproduced missing automatic bodies.13 final new tests pass; full982 passed/2skip27.21s. Independent review clear.
- Ruling: preserve two recent turns ahead of optional bodies, dropping older raw history first; complete low-ranked bodies then reduce before protected task state.
- Review fixed Unicode regression and live control leakage (description preview, alternate artifact tools); numeric gate avoids mistaking ID/time digits for facts. All pilots retained.
- Final tmp/live-summary-context-20261031-1cabbc6d/report.json: enabled6/6 tasks correct at last step, strict combined final JSON5/6; disabled0/6 fully correct; projection gates true/tool calls0 in all12.
- Context prep median23.43ms enabled/18.09ms disabled; enabled Agent median4.12s, range3.94–6.26s; remote summaries2.08–4.31s. Small sample, not general SLA.
- docs/conversation_summary_context.md records local model blocker, English-summary violation, final aggregation issue, pilot exclusions and reproducible commands. No default-model/.env/agent-core changes or commits.
