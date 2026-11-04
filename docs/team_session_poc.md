# Team Session coordination PoC — 2026-11-02

## Scope and decision

This is an experimental mechanism, not AgentTeamRunner or a production TeamCoordinator. No import/call of AgentRuntime or ToolCallingStepRunner; their source hashes and Planner/registry/router hashes match the pre-task snapshot. No production registration, dependencies, model defaults, .env, commits or pushes changed. Existing dirty work is retained.

New code: `app/agent/team_poc/session.py`, `coordination.py`; tests: `tests/agent/test_team_session_poc.py`; opt-in live driver: `scripts/validate_team_session_poc.py`. Implementation plan: `docs/superpowers/plans/2026-11-02-team-session-poc.md`.

## Mechanism

- Session owns its model history, revision inbox, call IDs, cumulative budgets and one run task. It uses existing model/message protocols, ToolRegistry, MCP adapters and ContextManager. No shared generic Agent Loop was extracted.
- A coordination request is an exclusive tool batch. Its ToolResult is retained before a structured event is emitted and the Worker waits. Tool invocation acknowledges acceptance, not a synchronous Lead response.
- The trial observes the event while Worker is unfinished. An independent Lead Session receives the original assignment and the problem. Its tools prepare a revision and start one helper. After the Lead decision settles, the Worker resumes the same history. Helper assignment text is model-authored.
- Revision checks discard stale model plans before tools. A revision during a tool preserves that completed result and produces correlated non-executed results for remaining obsolete calls. No automatic whole-session/tool replay.
- Each tools factory opens/uses/closes MCP in the Session owner task. Trial errors/cancellation cancel and join owned tasks. Completion is published after teardown; revisions are rejected during final settlement.
- This trial deliberately permits one coordination cycle and one helper. Additional coordination requests fail explicitly and cancel remaining work instead of waiting for a nonexistent second Lead cycle. It is not generalized dynamic task management.

## Deterministic evidence

RED: initial tests failed on missing module; coordination tests then failed on missing coordinator and a real final-settlement revision race. GREEN: implemented loop/trial and rejected changes while resources close. Live-discovered RED tests reproduced missing original assignment in Lead context and unsupported second-report waiting; fixed with bounded context and event monitoring. Helper-queue admission was also tested RED/GREEN.

- Final targeted: **22 passed** (`tmp/pytest-team-queue-green-20261102`). Includes two actual local filesystem MCP cases with controlled models (normal continuation and cancellation), owner-task identity, closed-adapter rejection, model/tool-boundary revision changes, call-ID validation, exclusive reports, resource settlement, Lead failure, cancellation, deadline, tool/revision limits and overflow retry without re-reading evidence.
- Final full working-tree regression: **1046 passed, 2 skipped, 22.63s** (`tmp/pytest-team-full-20261102d`). Existing skips retained. Startup:59 passed/1skip with pytest cache permission warning; later runs use `-p no:cacheprovider`.
- Independent final review repeated22 tests including actual MCP; no Critical/Important findings. The final22nd test enforces the reviewed helper-event-queue assumption.
- Syntax checks pass. Protected hashes: `tmp/team-poc-protected-hashes.json`.

## Real configured model and MCP evidence

Configured `qwen3.8-flash`, real read-only filesystem MCP, random synthetic facts only. SDK retries disabled. Per invocation cap36 model attempts, finite Session/trial/provider deadlines. No private transcripts or credentials are logged. All trials retained; **no full dynamic live trial passed strict deliverable acceptance**.

| Report under tmp/ | Continuity | Dynamic trial |
|---|---|---|
| live-team-session-b2d656e1/report.json | ModelRequestError before read | ModelRequestError before read; initial restricted execution path; not counted as functional evidence |
| live-team-session-7efbbad2/report.json | PASS,5.86s | FAIL,150.07s. Lead invented extra procedure/helper dependency; second report waited until deadline |
| live-team-session-3ef7609b/report.json | PASS,7.50s | FAIL,18.29s. Real Lead revised Worker/created helper and Worker returned correct JSON; helper added fields, strict mismatch |
| live-team-session-10df5979/report.json | PASS,7.15s | FAIL,26.10s. Same dynamic mechanism and correct Worker JSON; Lead requested verification prose conflicting with host JSON format, helper returned prose, JsonFormatError |

Across three model-connected runs:35 actual adapter attempts; initial blocked-path run adds2 attempts. These are iterative development trials with different prompts/code, **not a success-rate benchmark**. Continuity Worker reads exactly once in all3 connected trials. In both post-fix dynamic trials: Worker is unfinished at report, Lead tools actually prepare revision/start helper, Worker completes revision2 without rereading, helper independently uses MCP, and both closed adapters reject calls afterward in the owner task. Final detailed trace is `tmp/live-team-session-10df5979/report.json`.

The initial blocked-path report recorded `closed=false` because validator bookkeeping after an exceptional context exit was not reached; it does not establish a leaked connection. Instrumentation was moved into finally, and actual normal/cancel MCP tests plus later live probes verify cleanup.

## Findings and limits

1. Event-driven feedback and same-history continuation work without nested Runtime; the actual model made real coordination tool calls before Worker completion.
2. Lead must receive the host's original scope/output requirements, not only a member's report. Missing context caused genuine task expansion. The fixed prompt reduces this issue but is not a hard semantic permission boundary.
3. The single-cycle coordinator must fail visibly on unsupported new dependencies. It now does; future multi-cycle coordination is intentionally not implemented.
4. Independent helper delivery remains unvalidated: extra fields/prose violate strict JSON checks. In the final trial the Lead explicitly requested per-claim CONFIRMED/MISMATCH findings and no revised deliverable, conflicting with the host wrapper's JSON format; this is an unresolved host/Lead output-authority contract, not a clean test of an unambiguous JSON instruction. One helper also described wrapper content as extra file fields. A second model's review is not ground truth. No tool-free formatting-repair subsystem was added just to turn these trials green.
5. Call-ID dedup prevents replay of an accepted ID; **semantic dedup by tool+arguments is not enforced**. A fresh-ID repeat can execute. Current continuity evidence verifies no rereads in these fixtures, not arbitrary tool exactly-once semantics.
6. Cancellation/deadlines require cooperative model/tools/cleanup. No forced termination of cancellation-resistant dependencies is claimed. No process-restart/rebind recovery, durable inbox, production sandbox, parent artifact import, arbitrary team composition, shared filesystem write isolation or long-task reliability claim.

## Reproduce

```powershell
.venv/Scripts/python.exe -m pytest tests/agent/test_team_session_poc.py -q -p no:cacheprovider --basetemp ("tmp/pytest-team-" + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe scripts/validate_team_session_poc.py --live
```

The live command returns nonzero when any strict acceptance fails. Inspect both coordination events and deliverable assertions. Next decision: define the host/Lead/member output-authority boundary and determine whether multiple feedback cycles are needed before any production Runner integration.
