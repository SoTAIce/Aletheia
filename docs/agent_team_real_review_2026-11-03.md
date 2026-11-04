# Actual Agent Team use: long-term-memory code review

Date2026-11-03. This task used the existing product for real read-only code review, not a synthetic defect or new architecture component. Reviewed source files were never edited. One actual-use transport bug was fixed separately in Team Session (below).

## Authorization and task

User explicitly approved sending these five project sources to the configured qwen3.8-flash at dashscope.aliyuncs.com: memory_service.py, postgres_memory_store.py, memory_retriever.py, memory_store.py and models/longterm_memory.py. Automatic approval initially blocked the call pending destination-specific consent; no workaround was used. The user then approved the exact files/destination. No .env, secrets or conversation history were sent.

Both modes read byte-identical, hashed snapshots via real read-only filesystem MCP. Goal: identify actionable correctness/concurrency/user-isolation issues with evidence; no source edits or real database writes. Broader tasks covered five files; focused tasks covered MemoryService plus interface/model (the snapshot still contained only the authorized five files). Each pair used identical task text/model/output budget. Ordinary and team execution retain different built-in total budgets; these are practical comparisons, not equal-cost or statistical benchmarks.

One-off driver: `tmp/agent_team_usage_review.py`; reports retain SDK usage, actual model/tool calls, team actions/member events and raw deliverables. This is not a new product component. Root snapshots remain under each report directory. Source SHA-256 checks passed after runs. No commit/push/default-model/.env/dependency changes.

## Observations across attempts (all retained)

| Report under tmp/ | Scope / per-call output | Ordinary | Team |
|---|---|---|---|
| team-real-review-58c7ceb1/report.json | Five files /2048; initial driver40s request deadline |FAIL50.59s: read5 files then model timeout |FAIL3.20s: Lead API400 before member creation |
| team-real-review-c421f095/report.json | Five files /2048; existing configured90s deadline |FAIL8.12s: exhausted tool rounds and returned simulated tool calls instead of final protocol |FAIL34.00s: Worker consumed16 business-tool calls before submitting; no Reviewer |
| team-real-review-07395dc1/report.json | Focused three-file orchestration review /2048 |COMPLETED29.61s, report contains unsubstantiated findings |FAIL48.38s: Worker response hit2048 output ceiling; no Reviewer |
| team-real-review-73ac5b9b/report.json | Same focused task /4096 for both modes, per-invocation override only |COMPLETED53.01s,6 model requests,4 tool calls |FAIL114.68s,9 model requests,4 tool calls; Reviewer created but wrong execution command terminated team |

The final pair's actual SDK usage: ordinary42,745 input/3,645 output tokens; team57,334 input/7,685 output tokens. Input totals include cached tokens (ordinary18,432/team19,456). Task-level budgets are not equal, execution order/caching varies, and N is small; no general success-rate, latency or superiority claim is justified. Across all four attempts:56 API requests, plus1 diagnostic API400 replay; no further repeats planned.

### Lead allocation

No explosion of useless members was observed. In the final task the Lead used one Worker and one Reviewer. However, it delegated the whole review to one Worker and expanded concise instructions into a long checklist; there was no demonstrated benefit from dividing the code review into complementary subtasks. In the broad attempt, the Lead only issued create_worker/run_member before failure, so it did not intervene in the Worker's wasted exploration.

### Reviewer contribution

**No Reviewer actually executed in any of these code-review attempts.** In the last attempt the Worker submitted a draft; Lead created a Reviewer with a useful skeptical brief (including challenging the alleged positional-field corruption), but then issued run_member for that reviewer. The Coordinator correctly refused it with `reviewers must execute a version-bound review command` instead of bypassing target-version checks. No automatic command-correction opportunity was provided, so the whole team ended. Lead's skeptical intention is not evidence that independent review corrected or discovered anything.

### Repeated tools and wasted communication

- Broad team attempt:16 business calls,8 directory listings,2 exact duplicate reads by the same Worker, plus attempts to inspect the unavailable memory_vector_index implementation. Reviewer rereads cannot explain these: there was no active Reviewer.
- Focused2048 ordinary attempt:3 exact duplicate same-actor reads; duplication is not unique to Team mode.
- Final4096 pair: ordinary had0 exact duplicate reads but opened an extra authorized snapshot file outside the three-file focus; team reread memory_service once. No network search was used in this task.
- Final Lead instructions and Worker draft were verbose; team output tokens were over twice ordinary output tokens despite no completed reviewed deliverable. This is observed overhead, not a theoretical argument against teams.

`tmp/team-real-review-summary.json` records same-actor duplicate counts separately, avoiding mislabeling expected independent-review reads as waste.

## Human verification of model claims

The ordinary report's completion does not establish correctness. Several major claims were unsupported:

1. “Optimistic locking is ineffective because update has no separate expected_version parameter”: false for the current PostgreSQL implementation. postgres_memory_store.py:update carries record.version into `AND version = $8`; a separate parameter is not required. The focused scope omitted the implementation, which should have produced an uncertainty statement, not a confirmed defect. Worker also made this mistake.
2. “Validate saved against saved.user_id/saved.memory_id”: an unsafe suggestion from the2048 ordinary report. It makes ownership/identity checks tautological and weakens the existing expected-user/expected-ID guard. Generated IDs are intentional and current service uses keyword construction.
3. “No candidate content/category validation permits arbitrary values”: current real Extractor strips content, rejects empty or >300-character content and validates MemoryCategory. A defensive-check proposal is possible, but missing dependency context does not prove the claimed real pipeline defect. This verification read the local Extractor only; that file was not sent externally.
4. Worker's “positional MemoryRecord fields silently corrupt remember()”: not an established current service bug. remember constructs the record with keyword arguments. A caller supplying the wrong positional argument is an API ergonomics concern; frozen dataclasses still allow explicit IDs/timestamps in constructors. Lead itself flagged this claim for challenge, but Reviewer never ran.
5. Whitespace normalization is a policy suggestion, not a demonstrated cross-user overwrite. DB and vector filters use exact identifiers. The report additionally attributed keyword-query whitespace effects without accounting for the existing store's query.strip().

No verified improvement in final finding quality can be attributed to Team in this task. The concrete SQL/index race below was independently verified locally; neither mode delivered its exact interleaving as a reliable final finding. Their generic cross-database caveats should not be credited as a reproduced bug report.

## One concrete local consistency check

`tmp/verify_memory_review_races.py` executes the real LongTermMemoryService and MemoryVectorIndex code with controlled storage/embedding/Milvus-client stand-ins. No real PostgreSQL/Milvus instance was used. The actual index threading lock remains active. The storage stand-in delays return *after* a successful SQL-like commit, representing asynchronous result/connection-release timing.

- A commits version2 then pauses before its index update. B commits version3 and updates the index. A resumes and overwrites index content with version2: final database=B,v3; index=A.
- A commits an update and pauses. Concurrent forget deletes authoritative data and index. A resumes and upserts its old result: database absent; index=A.

Results: `tmp/memory-review-race-verification.json`. This concretizes a successful-call ordering risk in the documented cross-store consistency boundary; it is not proof of an observed production outage or database lost update. The retriever still rechecks authoritative rows, so an orphan vector is not automatically returned as valid user memory. Fixing the reviewed modules is outside this task.

## Small actual-use bug fix and checks

The API400 diagnostic replay returned: messages must contain “json” when using response_format=json_object. The configured JSON mode was already enabled, but Team's protocol instruction lacked this word; synthetic goals previously supplied it incidentally. Added “JSON” to the contracted Session system protocol, kept strict submission/validation unchanged, and added a regression with a plain code-review goal. This is a transport precondition fix, not disabling JSON mode or accepting invalid output.

RED19th V1 test reproduced it; GREEN19 V1 tests. Full regression: **1082 passed,2 skipped,29.74s**, fresh `tmp/pytest-team-real-use-json-fix-1103`. Startup59passed/1skip had the existing cache warning. All five reviewed sources are unchanged; previous reports and snapshots are retained.

## Practical conclusion

The current Team has not demonstrated a quality gain on this real code-review task. Its mechanics previously passed controlled tests; real use exposed allocation/verbosity, command-selection and budget problems. The next useful fixes are narrow operational ones: let Lead correct invalid commands without replaying completed work, keep instructions/drafts within output budgets, and discourage repeated exploration of known paths. No additional Agent components are warranted from these observations. Do not accept either mode's findings without source verification.

## Follow-up: bounded Lead command correction (2026-11-03)

Implemented only recovery of explicit pre-execution role/version/approval gate rejections in `team/coordinator.py`. Invalid reviewer `run_member`, invalid current-candidate `review`, and premature/stale `finish` return a `command_rejected` host event to the existing Lead Session. The Lead must issue a new valid command; no automatic conversion to review, implicit target/version selection, tool replay or approval bypass occurs. Rejections spend the existing action/model/time budgets. Permission failures, member/tool execution failures, cancellation, stale parent ownership and resource exhaustion remain terminal. Unknown members and assignment failures remain terminal in this narrow patch.

Four new failing tests reproduced the terminal-command issue before implementation; all four then passed. They verify retained Worker candidate/version/evidence, no Reviewer execution before correction, owner-task resource closure, strict review/finish gates, and finite repeated rejection. Scoped Session/contract/V1 tests: **62 passed**. Full fresh regression: **1086 passed, 2 skipped in 23.79s**, `tmp/pytest-team-command-full-1103`. No Runtime, ToolCallingStepRunner, routing, model defaults or reviewed memory sources changed.

One bounded real follow-up used the same approved snapshots/task/model and 4096 output limit: `tmp/team-real-review-7704ddee/report.json`. Ordinary completed in **22.48s / 6 model requests / 6 reads**; Team failed in **175.48s / 19 model requests / 11 reads**. Total25 additional requests; no additional attempts. Team input/output usage267,429/10,879 versus ordinary44,649/1,359. These are one paired attempt with unequal task-level budgets, not a statistical comparison.

Unlike the earlier attempts, the Reviewer actually executed: read the three target files independently, explored five additional paths (only approved snapshot files were available), then requested coordination about review scope. Lead narrowed the assignment. Lead then incorrectly used `run_member` for the Reviewer; the new rejection event was received and Lead corrected it to `review` for the Worker v1 candidate. The original Worker remained v1 with its original three reads; no Worker tool replay occurred. All four observed resource closures stayed in their owning tasks. No new source files beyond the five approved files were disclosed.

The resumed Reviewer's last provider response used all4096 completion tokens; the adapter never returned a ModelTurn and the Session/Runtime surfaced ExceptionGroup/TeamError. This is consistent with the adapter's strict truncated-response rejection, but the one-off driver did not retain the precise nested exception or provider finish_reason, so the exact cause is not independently established by this report. No version-bound approval or final Team deliverable exists. No extra diagnostic API replay was made.

Verified live: independent Reviewer execution, mid-review feedback, Lead adjustment, command correction and retained Worker evidence. Not verified live on this task: completed Reviewer judgment, targeted Worker rework, final delivery, or better factual quality. Ordinary output again included weak findings (documented index-sync behavior treated as a defect and redundant parameter validation framed as a defect). Next obstacles are bounded review output/context and observability of nested failure causes; this patch does not introduce silent truncation, automatic business-tool retries or loosen acceptance. The five original source hashes were rechecked unchanged. No commit/push was created.

## Reviewer output and exception reliability follow-up (2026-11-03)

Kept the Team architecture. The shared adapter now raises a backward-compatible `ModelOutputLimitError` subclass of ModelResponseError on explicit provider finish_reason=length, preserving only validated token counts/configured output limit. It still rejects partial responses before tool parsing. Normal Runner behavior and model defaults are unchanged.

The host-owned REVIEW schema and validator limit feedback to2000 characters (not tokens), requesting concise findings rather than a duplicate report. No content is silently cut and no approval is invented. Only production Reviewer Sessions opt into output-truncation repair. A rejected truncated response may consume the existing max_format_repairs=2 budget to generate a fresh compact submission from retained evidence; only submit_result is available. Tool attempts during this repair are rejected before execution. Other failures and Worker/Lead truncation remain terminal. Existing team model/action/time budgets still apply.

New diagnostics retain bounded nested ExceptionGroup children and causes, exact exception types, explicit output-limit metadata, Session identity/version and counters. Session failure events and Team state retain safe records; Coordinator emits safe structured logging before state disposal. No raw exception messages, provider bodies, partial responses or traceback locals are logged. User-facing TeamError includes nested type names; original causes still chain. The one-off real driver also records finish_reasons, adapter failure details and observed state diagnostics, correcting its earlier observability gap.

TDD:5 initial RED cases reproduced missing typing/repair/schema/diagnostics;7 added tests cover safe usage, feedback bounds, finite grouped failures/log privacy, no business tools in repair, nontruncation terminal behavior and no Worker retries. Scoped167 passed. Independent read-only review found no important issues and ran105 targeted tests. Fresh full suite: **1093 passed, 2 skipped in24.00s**, `tmp/pytest-team-output-full-1103`. Syntax checks passed.

One real Team-only continuation, using the same five approved snapshots, focused goal and4096 output cap: **COMPLETED153.12s,15 model calls,6 business reads**, report `tmp/team-real-review-4c154c6d/report.json`. Worker read each required file once, Lead revised the draft without new reads, Reviewer independently read the same three files once. Reviewer first submitted >2000-character feedback: rejected by host validation, then corrected to **1525 characters** with only submit_result available. Review approved Worker v2; Lead finished that version. All4 observed connection/resource scopes closed in the owning task. Usage131,375 input/9,289 output tokens; no same-member duplicate read. No additional diagnostics API replay or repeat run was made.

This live run verifies strict overlength-feedback repair, independent current-version review and final delivery. **No provider length truncation occurred** (14 tool_calls/1 stop); typed length recovery, nested-error logging and repeat-exhaustion are deterministic-test evidence only. This single completed run after earlier failures does not establish a general success rate or speed/quality advantage.

Factual quality remains separate: final report calls lack of service-level updated_at advancement a confirmed defect, while the actual PostgreSQL update sets updated_at=NOW(). Its claim of an unbounded default retrieve history also ignores actual retrieve limit=5. Those implementations were inspected locally to verify findings; no additional files were sent to the provider. Reviewer approval is not factual ground truth and these suggestions should not be applied blindly. No reviewed memory sources/default/.env/dependencies, Runtime or ToolCallingStepRunner were modified. No commit/push.
