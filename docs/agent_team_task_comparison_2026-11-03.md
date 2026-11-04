# Frozen Agent Team V1: real task comparison

Frozen local commit:9138ac1e7e5a5fbc4d31a7503bec325fc8a3f683. Exact candidate and working checkout both independently passed1093 tests/2skipped. Commit includes required existing Turn/archive/summary dependencies and full regression tests; excludes unfinished long-term injection/projection and user-only annotations. No push.

## Protocol fixed before model calls

Three actual read-only maintenance deliverables on the five already-authorized long-term-memory source files: review-dispute adjudication, incident-response matrix, and overlapping-operation consistency review. These are bounded structured maintenance tasks, not repository-wide coding or a general benchmark. No seeded code defects, private transcripts, extra sources or database writes.

Task definitions and exact offline answers are frozen in `tmp/team_v1_task_specs.py`; only question/schema goes to models. No grading feedback or semantic answer oracle is supplied to either mode, no failed-case reruns. Each final answer is graded separately for task completion, strict output/evidence shape and exact typed facts. Correct draft content from a failed task is diagnostic only, not final-delivery success.

Frozen ground truth:
- Review disputes (8facts): timestamp_stale/CAS_missing/retrieve_unbounded/identity_check_redundant are refuted; delete_user_scoped supported; retrieve default5, default candidate_k15, expected-version SQL parameter8. Evidence: PostgresMemoryStore.update sets updated_at=NOW(), guards version=$8 and user_id=$7/memory_id=$6; delete guards both identifiers. MemoryRetriever.retrieve defaults5 and computes min(100,max(limit*3,10)). LongTermMemoryService checks saved record identity against requested identity.
- Failure runbook (20facts): deleted/missing forget index failure raises MemoryIndexSyncError with database_changed true/false respectively and index_called true. ADD post-save index failure raises MemoryIndexSyncError/database_changed true/index_called true. SQL CAS failure raises ValueError, no database_changed attribute (null), index_called false. Cancellation during index propagates CancelledError, no database_changed attribute, index_called true. Blind remember replay is not the safe recovery in any case. These branches are covered by existing service regression tests; no live DB run is claimed.
- Consistency review (12facts): supplied successful-operation schedules have final DB B/v3,index A,recall B for late_update; DB absent,index A,recall absent for late_after_forget. CAS does not order later index writes; orphan vector is not returned as authoritative content; rebuild source is PostgreSQL; blind remember retry is not recommended. Concrete service/index controlled interleavings were reverified by `tmp/verify_memory_review_races.py` before calls; retriever revalidates current DB rows. This uses explicit index semantics, not an unfounded claim about unseen implementations or a production outage.

Each pair uses identical snapshot/question/model/temperature0/per-call4096 output cap and read_file-only MCP registry. Both receive16 business-call allowance,32 provider attempts/run,250s step deadline, no Replan; Team has its normal additional coordination/review requirements. Ordinary max_tool_rounds is explicitly16 via existing registry injection so five-file tasks are not forced to fail at the default4rounds. No default configuration changes. Total provider-call ceiling120 across six runs, no provider auto-retries. Actual total cost remains unequal because team orchestration is work; compare measured calls/tokens/latency, not nominal budget equality. Alternate execution order by task to reduce a fixed first/second bias; single sample per task is not statistical evidence.

Only frozen-candidate application code is imported during validation; credentials load locally from the existing workspace .env, which is never sent. Source snapshot contains only the five previously approved files and excludes answers, test scripts and this report. All failed outputs and SDK finish_reason/usage/diagnostics are retained locally.

## Results

Completed one predeclared three-pair run (42 actual provider requests, no model-task reruns).

| Task | Ordinary Runner | Agent Team |
| --- | --- | --- |
| Review disputes | PASS, 8/8 facts, 34.45s, 4 requests | FAILED delivery, 85.47s, 13 requests |
| Failure runbook | FAILED output contract, 20.61s, 3 requests | FAILED JSON parsing, 67.44s, 5 requests |
| Consistency review | PASS, 12/12 facts, 33.89s, 4 requests | FAILED delivery, 87.59s, 13 requests |

Strict end-to-end delivery: ordinary 2/3, Team 0/3. No completed Team answer exists for factual-accuracy comparison; invalid drafts scored zero for delivery are not evidence that all their facts were false. No Team accuracy advantage is demonstrated by these three tasks.

Ordinary totals: 11 requests, 88.95s, 15 reads, 106778 prompt / 5011 completion tokens. Team totals: 31 requests, 240.50s, 24 reads, 309180 prompt / 14767 completion tokens. Prompt totals include cached inputs; monetary cost was not calculated.

### Observed failure mechanisms

- Review disputes: Reviewer exceeded the 2000-character feedback limit, then violated submit_result argument shape; bounded repair exhausted. Worker candidate also contained an unterminated JSON string.
- Failure runbook: ordinary final outcome failed the required status/content/reason contract; Team Worker native response raised ModelResponseError caused by JSONDecodeError before a usable candidate.
- Consistency review: Reviewer repeatedly exceeded the feedback character limit and exhausted bounded repair. Worker candidate had invalid inner JSON delimiters.
- No Team run reached accepted finish or successful version-bound review approval. Reviewer narrative claims of approval are not accepted protocol completion or demonstrated independent correction.
- No provider finish_reason=length occurred: these runs do not verify provider truncation recovery. Character-limit repair and JSON serialization are the observed reliability bottlenecks.

Where review was reached, Lead created one Worker and one Reviewer. Same-member excess reads were zero in all six runs; extra Team reads were independent review reads. All 11 MCP resource scopes closed in their owning tasks. No architecture redesign is justified by this small comparison; preserve the frozen baseline and isolate any subsequent reliability repair in a separate task/commit.

### Evidence and limitations

Artifacts: `tmp/team-task-comparison-1edb2b76/report.json` and `summary.json`; fixed tasks `tmp/team_v1_task_specs.py`; driver `tmp/run_team_v1_task_comparison.py`; controlled consistency schedules `tmp/verify_memory_review_races.py`. The driver invokes paid APIs: do not rerun without a separately scoped validation request. One earlier setup assertion failed before any API because of LF/CRLF comparison; only source byte normalization was corrected. No model-task retry was hidden.

Three structured read-only maintenance tasks, one sample per mode/task, cannot establish general coding performance or statistical superiority. Exact typed factual fields and evidence shape were graded; free-form prose was not comprehensively scored. Independent static review covered commit packaging; final comparison results were checked in the main process. No live database operation is claimed. No production code, model defaults, dependencies or reviewed memory sources were changed during this evaluation. No push or additional commit.

