# AgentTeamRunner V1

Implemented2026-11-02. This is production-path integration of the verified Session/contract mechanism, not another independent experiment. The existing single-Agent path remains the default.

## Run

```powershell
.venv/Scripts/python.exe -m app.agent "Read the supplied files and produce an independently reviewed report" --execution-mode team --filesystem-root D:/your/readable/folder --no-knowledge
```

The default final output is JSON `{ "answer": "..." }` with a nonempty answer. Ordinary execution uses `--execution-mode single` (the default). Existing model configuration is used unchanged.

For same-process pause/resume and custom trusted permissions/contracts, keep one service alive:

```python
from app.services.rag_agent_service import RagAgentService
from app.agent.team.models import TeamOptions

async with RagAgentService(execution_mode="team", team_options=TeamOptions()) as service:
    result = await service.query_result("Your goal", "session-1", user_id="alice")
    # If blocked, present result.content to the user, then use their real answer:
    # result = await service.query_result(answer, "session-1", user_id="alice",
    #                                    resume_task_id=result.task_id)
```

Do not invent user answers. The CLI exits on BLOCKED like the existing CLI; restarting it restores history, not live team execution. Use the long-lived service for resumable interaction. User clarifications consume the existing Runtime's execution/replanning limits, which are configurable.

`TeamOptions.output_contract` accepts an immutable `app.agent.team.contracts.JsonContract`: exact str/int/bool fields plus an optional trusted semantic validator that raises ContractError. Lead instructions cannot change this validator or serializer. The host gate is distinct from member shape validation: a fact mismatch returns a `host_rejected` event for targeted rework, not a formatting retry. Reviews are opinions; only configured host checks provide deterministic domain validation.

## Architecture and scope

- `agent_team_runner.py`: independent StepRunner implementation and `register_team_runner` helper. It uses the existing RunnerRegistry/CapabilityRoutingPolicy. No calls/imports of ToolCallingStepRunner or nested AgentRuntime.
- `team/session.py`, `contracts.py`: promoted PoC loop/contracts, preserving old PoC imports through compatibility exports. Optional boundary mode returns a checkpoint after coordination/submission and closes its tool connections. Rebinding restores model/tool factories and continues the same history/counters; it never repeats accepted tools automatically.
- `team/coordinator.py`: an independent Lead Session emits validated commands. A deterministic coordinator creates workers/reviewers, assigns/revises tasks, runs one member at a time, handles feedback, conducts current-version review/rework, asks the user and submits the accepted final result.
- `team/models.py`: trusted immutable TeamOptions, shared budgets, bounded same-process TeamStore, per-member state and explicit host integration callbacks.
- `rag_agent_service.py`: explicit execution mode, separate member tool/ContextManager/WorkingMemory assembly, state retention across blocked requests, source import at final commit and cleanup.
- Runtime adds only terminal classification for an outer team timeout, preventing side-effect replay through Replan. TaskState/WorkingMemory semantics and the ordinary timeout path are unchanged. CLI adds execution-mode selection.

Workers may report partial facts or missing dependencies via coordination events; Lead can hand those facts to other members. Final candidates must match the host shape. Member composition/assignments/review focus are model-authored within the host envelope, not a hardcoded one-worker/reviewer sequence. V1 deliberately schedules sequentially and requires independent current-version review for final acceptance.

Default registry/policy and ToolCallingStepRunner source bytes match pre-integration hashes `tmp/team-v1-before.json`. Additional registry assembly is opt-in. Applications can register both modes and supply a trusted RequirementsProvider for mixed routing; no LLM complexity classifier or failure-triggered silent mode switch was introduced. For custom registries, call `register_team_runner` with a task-scoped TeamStore and TeamEnvironment; production binding requires a parent execution ownership guard.

## Permissions, evidence and lifecycle

- Member tools are the intersection of the host allowlist, actually available tools, and Lead-requested tool names. Default team tools are read/search/RAG/clock only. File writes require both explicit `TeamOptions.allowed_tools` permission for write_file and an existing writable MCP root; neither Lead prose nor host output formatting grants access.
- Each member has private WorkingMemory and ContextManager. Conversation archives/long-term memory are not injected wholesale into members. Parent constraints are supplied as immutable instructions; parent state is never handed to members as mutable authority. Tools remain responsible for argument/path authorization as in the existing infrastructure.
- Tool connections enter/use/exit in the same asyncio task for each member run. Checkpoints and user pauses leave no live child tasks/MCP sessions. No arbitrary shared parent ToolRegistry is used for member execution.
- A parent ownership guard checks task/goal/plan/active-step/state-version before/after external model/tools and before synchronous source import. Parent StepExecutor still owns the final commit token. Foreign private source refs are rejected; approved evidence is mapped into parent Resources, with namespaced trace call IDs.
- A reviewer receives the exact candidate version and bounded host-provided tool evidence previews (explicit truncation markers), and may independently read sources using its own granted tools. All current reviews must approve. Revising a worker invalidates its reviews; stale approvals cannot finish a new candidate.
- Same-task resume matches the exact answered team question, goal revision and old step. Existing TaskState requires a replacement plan after answers, so the blocked team's state migrates once to the first team step in that replacement plan. It retains member identities/evidence/limits and consumes the specific answer once; it does not reopen arbitrary old teams.
- Shared limits cover all Lead/member model requests, estimated request/reserve tokens, business tool calls, commands, member count, member revisions and active elapsed time. Format repairs are bounded and submit-only. Lead event turns use the command limit, not the smaller member revision limit. Budgets do not reset at rebind or user resume.
- Exceptional tool/provider failures, configuration errors and budget exhaustion terminate without automatic team replay; ordinary negative tool results can be reported to Lead for adjustment. External cancellation propagates; ambiguous effects are not automatically retried. User pauses exclude user think time from active team time.

## Verification

- New V1 tests:18 passed. Including prior PoC tests: **57 passed**, independently repeated57 with no remaining serious review findings.
- Final full working-tree regression: **1081 passed,2 skipped,24.56s**, fresh `tmp/pytest-team-v1-full-release`. Earlier gates also passed. Syntax checks passed; no dependencies/config/default changes or commit/push.
- Tests cover routed service completion and default-path regression, dynamic creation/assignment, intermediate coordination, private RAG evidence review/import, host rejection followed by reviewed rework, permission failure, shared budgets, external cancellation, internal/outer timeouts, stale execution ownership, same-user/session pause/rebind/new-plan migration and no repeated completed clock operation.
- Independent review found and drove fixes for Lead/member revision-budget conflation, missing reviewer evidence, and precommit stale source import. Minor per-task repair metric accumulation was corrected. Historical RED/failed trials are retained.

### Real Planner/model/Runtime/MCP

Configured qwen3.8-flash and actual filesystem MCP; isolated synthetic nonce/amount fixtures only. `scripts/validate_agent_team_live.py` traces actual calls without mocking model/tool responses. An observer wraps the real service factories only to record ownership, tool output and routes. All accepted results satisfy the exact host JSON/semantic assertions, real independent reviewer reads and closed-adapter probes.

| Report under tmp/ | Actual outcome |
|---|---|
| live-agent-team-45da5dae/report.json | Both tasks failed before team execution: real Planner exceeded its existing300-character step limit.2 calls; failure retained. |
| live-agent-team-76f31678/report.json | Clarified test asks a short single macro-step; completePASS64.39s and user pause/resumePASS69.46s;28calls. |
| live-agent-team-67716f0d/report.json | Seeded wrong-amount negative control: reviewer rejects, same Worker is reassigned, new candidate reviewed, strict final resultPASS98.73s;19calls. |
| live-agent-team-621cd4b3/report.json | Final integration hardening rerun: actual user pause/close/rebind/new-plan resumePASS41.32s;15calls. |

Total64 real calls across bounded invocations, including the failed Planner pilot. Script cap96 calls/invocation, finite provider/team timeouts, no SDK retries. Earlier tests preceded some error-path hardening; final pause rerun exercises the hardened service/ownership/resume path (the later change only corrects diagnostic repair counts/docstrings). No original Planner length check or final assertion was weakened. These are functional scenarios, not a statistical success rate or latency guarantee. The rework case is explicitly seeded; it does not demonstrate spontaneous model error discovery.

```powershell
.venv/Scripts/python.exe -m pytest tests/agent/test_team_v1.py -q -p no:cacheprovider --basetemp ("tmp/pytest-team-v1-" + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe scripts/validate_agent_team_live.py --live
```

## Remaining limitations

Same-process/service-instance recovery only; no recursive teams, complex parallel scheduling, durable execution checkpoints or distributed coordination. Async cancellation depends on cooperative dependencies. No general tool exactly-once guarantee: a model can intentionally issue a fresh-ID repeat; observed no-reread assertions apply to these fixtures, and formatting repair is host-enforced tool-free. Context/token counts are estimates. Long team histories can fail closed on the existing context budget rather than be silently truncated.

Review prose and business correctness remain model-dependent without a domain host validator. Default JSON answer check enforces nonempty text, not arbitrary factual truth. Permissions rely on configured tool implementations/MCP roots, not an OS sandbox or automatic multi-tenant filesystem policy. Large evidence previews can be truncated; reviewer must fetch authorized sources or report uncertainty. No claim of production-scale reliability or restart recovery is made.
