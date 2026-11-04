# Team PoC: output ownership and bounded multi-round review

Date:2026-11-02. Scope remains experimental; no AgentTeamRunner/production routing, nested Runtime, new dependencies or changes to existing Runtime/ToolCallingStepRunner/Planner/Multi-Runner V1. All prior dirty work and failed live reports are preserved. Plan: `docs/superpowers/plans/2026-11-02-team-contract-review.md`.

## Three ownership boundaries

| Boundary | Owner | Enforced behavior |
|---|---|---|
| Final output | Host application | Frozen `JsonContract`, exact keys and scalar types, independent semantic callback, host canonical JSON serializer. Only host serializes an accepted final result. |
| Internal assignment/review focus | Lead | Can revise work and choose narrative review focus; cannot supply/replace schemas, validators or final serializer through the decision protocol. |
| Member output protocol | Host at Session construction | `submit_result(payload)` is validated in Python. Worker candidates and Lead decisions await acceptance; reviews carry candidate_version/decision/feedback. Review prose belongs in feedback, not a replacement transport format. |

This is not a generic JSON Schema engine: supported fields are exact str/int/bool scalars. Boolean is not accepted as integer. Objects are serialized by the host after validation, not by trusting LLM text. Schema hints in tools/prompts aid usability; actual enforcement is Python validation. Contracts/check callbacks are trusted same-process application inputs, not a sandbox against hostile Python callers.

## Session and review changes

- `contracts.py`: small immutable contract and canonical serializer, optional host semantic check.
- `session.py`: opt-in contracted Sessions retain legacy uncontracted behavior. Plain text is rejected as a submission; invalid payloads get bounded local protocol repair. During repair only submit_result is exposed AND checked before invocation; business tools cannot replay even under a new call ID in that repair. Counters remain cumulative across revisions.
- Reviewable submissions emit candidate_submitted while retaining the same task/resources/history. Host accept requires current task version AND the actual currently waiting candidate version. Revising invalidates that candidate immediately. Original Session only completes after acceptance and teardown.
- `reviews.py`: small bounded ReviewTrial reuses the same Worker and independent Lead across feedback/rework. Each candidate gets a fresh independent reviewer (deliberately avoids stale reviewer context). Review must target the exact candidate version; approval requires current review approval, Lead acceptance and independent host semantic validation.
- A reviewer/Lead vote alone never proves truth. Review rejection or host mismatch can trigger Lead-authored rework on the same Worker. No automatic reset/re-execution of the Worker or its completed reads. Current Worker/Lead failures interrupt and join an in-flight reviewer.
- Old CoordinationTrial and its original live script remain historical, unchanged. This extension adds bounded cycles, not a general task board, membership service or persistent inbox.

## Deterministic verification

TDD RED/GREEN demonstrated: exact JSON rejection, text-to-tool submission repair, forbidden business calls during repair, immutable contract access, current-candidate acceptance, two coordination requests plus rejected draft/rework/acceptance, stale review rejection, host semantic veto despite model approval, reviewer veto, cycle exhaustion, cancellation in Lead/review waits, cumulative repair budget and real MCP lifetime through candidate rework/cancellation.

A RED test exposed acceptance of an old candidate under a newly incremented task version; fixed by a separate waiting-candidate version invalidated on revise. Independent review identified bounded wasted work if Worker failed during review; a RED test reproduced it, and review execution now races Worker/Lead failure and cancels promptly.

- Final PoC tests: **39 passed** (17 new contract/review tests plus22 existing Session tests), fresh `tmp/pytest-team-contract-mcp-final`.
- Independent final review: **39 passed**, no blocking defects, fresh `tmp/pytest-review-contract-independent2-20261102`.
- Full working-tree regression: **1063 passed,2 skipped,24.51s**, fresh `tmp/pytest-team-contract-full-final-a`.
- Startup gate59passed/1skip with existing pytest cache permission warning; subsequent tests disabled cacheprovider.
- Protected source hashes match `tmp/team-poc-protected-hashes.json`. Syntax checks passed. No commit/push/default-model/.env changes.

## Real-model verification

Real configured qwen3.8-flash and read-only filesystem MCP; unique synthetic nonce/amount fixtures. SDK retries disabled,36 actual adapter attempts maximum per script invocation, finite per-Session/trial/provider deadlines. Driver `scripts/validate_team_contract_review.py` retains raw event/tool/model traces and nonzero exit on failed assertions.

Initial report: `tmp/live-team-contract-06ad674e/report.json`, **28 real adapter calls**, both cases passed all strict assertions:

| Case | Observed | Evidence |
|---|---|---|
| Directed multi-feedback | Same Worker reports at revisions1 and2; same Lead supplies two adjustments; Worker submits v3; independent reviewer approves; host final JSON exactly matches source |3 cycles,81.90s; Worker reads once; Lead1 format repair/reviewer1 repair; all MCP adapters closed and owner-task checked |
| Seeded negative-control rework | Worker requests authorization, submits intentionally wrong amount at v2, real reviewer rejects; Lead orders rework; same Worker submits corrected v3; fresh reviewer approves; host accepts exact JSON |3 cycles,82.11s; Worker reads once; review v2=revise, v3=approve; all MCP adapters closed and owner-task checked |

The multi-feedback fixture explicitly requests a second question. The rework fixture explicitly seeds one wrong candidate and requires it reach review unchanged. **These are directed mechanism/negative-control tests, not evidence of spontaneous task decomposition or a measured autonomous error-discovery success rate.** No model/tool responses were mocked. Narrative feedback is preserved inside its allowed field; local formatting repair only resubmits output and never repeats business tools.

The initial live process began before the failure-monitor refinement; its normal-path protocol/acceptance logic was unchanged. A final-source rework rerun is recorded separately below rather than conflating snapshots.

Final-source rerun: `tmp/live-team-contract-1275968c/report.json`, **15 calls, PASS,83.37s**,3 cycles; rejected v2 and accepted reviewed v3, exact final host JSON, one Worker read and owner-task/closed-adapter checks all passed. Total this task:43 adapter calls across the two bounded invocations. All earlier failed reports from the previous PoC remain intact.

## Limits / what is only deterministically proven

- Malicious/conflicting Lead output requirements, stale review rejection, falsely approving reviewer+Lead, cycle exhaustion and cancellation/failure races are deterministic adversarial tests; real live trials do not prove universal resistance.
- Contracts guarantee shape and host-defined checks, not truth for arbitrary tasks. Here host knows exact fixture facts. Real reviewer explanations still contain imperfect inferences about wrapper data and earlier defect history; source traces/host gates, not model confidence, own acceptance.
- Same Worker/Lead survive within one in-process run, with bounded revisions/cycles/model/tool/repair counts. No process-restart, durable recovery, arbitrary team membership, recursive delegation or production artifact-permission isolation.
- General semantic dedup of business tool+arguments remains unimplemented. The loop never automatically replays; format repair cannot invoke business tools; new legitimate work revisions can request new calls, which require separate policy in production.
- Async cancellation is cooperative. Per-trial latency was about82s in these fixtures; no general performance or production reliability claim.

## Reproduce

```powershell
.venv/Scripts/python.exe -m pytest tests/agent/test_team_contract_review.py tests/agent/test_team_session_poc.py -q -p no:cacheprovider --basetemp ("tmp/pytest-contract-" + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe scripts/validate_team_contract_review.py --live
```

No production Runner integration is authorized by these results. Next work should be decided from the contract/multi-cycle experiment results, not silently expand this harness into a full TeamCoordinator.
