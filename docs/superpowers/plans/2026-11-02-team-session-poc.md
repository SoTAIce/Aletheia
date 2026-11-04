# Team Session Dynamic Coordination PoC

Scope: implement two authorized experiments, not AgentTeamRunner or a production TeamCoordinator. Preserve Runtime, ToolCallingStepRunner, Planner, routing, defaults, dependencies and all unrelated dirty files. Work in place using additive files to validate the actual uncommitted infrastructure; no commit/worktree or push.

1. RED: deterministic tests for an isolated Session's continuity and blocking coordination report, real owner-task resource lifetime, revision changes during model/tool waits, duplicate IDs, cancellation and finite budgets.
2. GREEN: app/agent/team_poc/session.py with independent model/tool loop, existing ContextManager/ToolRegistry protocols, report tool, revision-checked inbox and structured events. No Runtime/Runner imports. Resource factories enter/exit in run's owning task.
3. RED/GREEN: minimal app/agent/team_poc/coordination.py harness with Lead-facing tools (revise_worker and start_helper), one waiting-worker event and one Lead decision session. No general task board/messaging framework. Demonstrate helper creation only after actual Worker feedback, before Worker completion. Cancel/join every owned task on errors.
4. Run fresh scoped and full regression. Request independent code review, fix material findings with tests.
5. scripts/validate_team_session_poc.py: opt-in bounded real configured model + real filesystem MCP, unique synthetic nonce fixtures, two scenarios, model/tool/event traces and explicit no-reread/late-state/connection assertions. Never log keys or private transcripts. Retain failures and do not convert pilots into pass statistics.
6. Record exact test/live evidence and limitations in docs/team_session_poc.md, feature_list.json, progress.md and session-handoff.md.

Acceptance: Lead sees a coordination request while Worker is waiting and unfinished; updates its task revision and starts a helper via actual tool decisions; Worker continues same history without re-reading completed evidence. Old model calls cannot execute tools; revision changes between batch calls skip remaining old calls with correlated replies. Cancellation during waits/model/tools closes resources and cannot publish successful late results. Finite model/tool/revision/deadline limits. Deterministic and real-service results reported separately.

Deferred: persistence/restart/rebind, production routing, AgentTeamRunner, generalized task board, arbitrary member creation, recursive teams, production permission isolation, tool exactly-once after ambiguous external failures.
