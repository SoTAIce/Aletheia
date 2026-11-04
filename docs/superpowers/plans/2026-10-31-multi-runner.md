# Multi-Runner Infrastructure Implementation Plan

> Execute inline with writing-plans/TDD/executing-plans and final independent requesting-code-review. User approves direct implementation. No additional planning approval, commits/pushes, real APIs, dependencies or second production Runner.

**Goal:** Register/bind/routable StepRunners while retaining default ToolCallingStepRunner execution and existing state semantics.
**Architecture:** RunnerRegistry snapshots trusted definitions and synchronous factories; RunnerDependencies supplies request-scoped clients/settings. RoutingStepRunner implements existing StepRunner. A separate trusted requirements_provider derives immutable RunnerRequirements; RoutingPolicy chooses but cannot relax requirements. Runtime init/rebind share one assembly function.
**Tech Stack:** Existing Python dataclasses/typing/asyncio/pytest, no dependencies.
**Spec:** User-approved multi-Runner proposal plus five hardening requirements in the current request.

## Scope and rulings
- Preserve dirty user edits; snapshot tmp/multi-runner-baseline-20261031. Work in place because prerequisites are uncommitted; no reset/stash/worktree migration.
- Own new runner_registry.py/runner_router.py/routing_step_runner.py, minimal Runtime/StepExecutor/StepRunner/service changes, tests/docs/harness. Do not edit Planner/TaskState/WorkingMemory/ToolCallingStepRunner/default models/Memory modules.
- First production registry has only tool_calling with capability tool_calling. Two fake Runners in tests are not production features.
- RunnerDefinition(id,description,capabilities), RunnerRequirements(capabilities), RouteDecision(runner_id,reason) are frozen. Requirements come from separately injected application provider, never from RouteDecision or model text.
- Definitions/factories are snapshotted per Runtime; eager synchronous factory construction per model/tool binding preserves legacy initialization validation. Factories must not own/open shared client lifetimes. Runtime stores original limits explicitly, no access to runner private limits during rebind.
- Policy is synchronous and receives read-only context/definitions/requirements. Default selects tool_calling when compatible; requirement matches use explicit preferred IDs, otherwise unique candidate; ambiguity/unknown/mismatch fail closed. No LLM routing, task-string keywords or fallback retry.
- StepRoutingError derives StepRunError, is recorded as step failure and terminal in Runtime without Replan. Existing stale token checks still control commits.
- Rebind builds new components before atomic replacement, only when idle and no RUNNING step. Old routing bindings are invalidated after successful replacement. Cancellation requested while a Runner awaits must propagate even if the Runner suppresses CancelledError and returns output; no cancelled output commit.
- Stale result/cancellation must not cause Runtime exception cleanup to fail a newer task state. Preserve timeout/replan limits and ordinary failure categories otherwise. Cancellation is cooperative; do not claim rollback of external effects or forcible termination of hostile coroutines.
- Existing StepExecutionReceipt/AgentRunResult/archive schemas and Planner string-step protocol remain unchanged. Route diagnostics are task-scoped read-only events with task/step/goal/plan identity.

## Review focus
Forged/relaxed capability decisions; mutable registry after binding; failed rebind partial updates; swallowed cancellation/timeout; late success/failure after goal revision or binding invalidation. Test each explicitly.

## Stage1: Registry and routing (TDD)
- [x] RED duplicate/invalid registration, immutable snapshot, dependency injection/factory limits, default registry only one production Runner.
- [x] Implement definition/limits/dependencies and registry/default factory.
- [x] RED dynamic fake routing, trusted requirements mismatch even when decision says nothing, unknown/ambiguous routes, invalid/async policy/provider, no secondary fallback, route diagnostics.
- [x] Implement policy + dispatch with binding invalidation and cancellation guard; GREEN scoped tests.

## Stage2: Runtime/state integration (TDD)
- [x] RED terminal StepRoutingError/no RUNNING or Replan, same init/rebind assembly, atomic failed rebind, fresh model/tool dependencies, no binding replacement during execution, old output/stale cleanup/cancellation/timeout protections.
- [x] Integrate default registry/options through Runtime and service; minimal StepExecutor classification/cancellation checks. Preserve legacy public arguments and default runner behavior/metrics.
- [x] GREEN core/runtime/service/lifecycle/MCP tests and full regression.

## Stage3: Review/documentation
- [x] Independent read-only review; RED/GREEN any substantive findings; final regression and compilation/whitespace checks.
- [x] Document interfaces, trusted requirements, source changes, actual tests and second-Runner steps. Update feature/progress/handoff. No APIs/commit/push.

## Evidence
Startup harness59passed/1skip; fresh full baseline982passed/2skip. Registry/dispatch23 RED then23green. Integration10RED then broad123green. Reviewer stale generic output/service cleanup findings reproduced with3 RED tests, fixed and broad127green.
Final42 new tests passed and independently42 passed; full1024passed/2skip23.31s. Compile and scoped whitespace checks pass. Exact direct-vs-routed model-message/output parity verified.
Ruling: stale ownership applies to all result forms, including implementation exceptions, invalid output and service interaction cleanup. Finish only the superseded Turn without failing a newer task; no TaskState semantic changes.
Documentation:docs/multi_runner_infrastructure.md. No second production Runner, LLM routing, APIs, dependency/model/default/.env changes or commits/pushes.
