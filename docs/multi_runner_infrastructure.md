# Multi-Runner infrastructure, phase1

Implemented2026-10-31. This isolated commit is based on HEAD41ce1fc and excludes separately uncommitted Memory/Turn/summary features. See multi_runner_live_validation.md for actual commit-tree verification. Only ToolCallingStepRunner is registered in production. No LLM router, second production Runner, new framework/dependency, plan schema or archive/result schema change.

## Execution path

```text
Planner -> Executor -> AgentRuntime -> StepExecutor -> RoutingStepRunner
                                                        | trusted requirements provider
                                                        | RoutingPolicy selection
                                                        | registry/binding validation
                                                        v
                                                 ToolCallingStepRunner
                                                        v
                                                  existing ToolRegistry
```

StepExecutor still owns execution tokens, context/output validation and WorkingMemory commit. Runtime still owns sequential progress, existing step/planning deadlines, bounded execution/replan counts and interaction lifecycle. Tools are not re-registered into another tool system.

## Interfaces and ownership

- RunnerDefinition(runner_id,description,capabilities): frozen trusted registration metadata; capabilities must be a frozenset of nonempty strings.
- RunnerRegistry.register(definition,factory): rejects duplicate/invalid registration. Factories synchronously construct lightweight Runner objects from RunnerDependencies. RunnerRegistry.snapshot() isolates active Runtime declarations from subsequent application registration.
- RunnerDependencies(model,tool_registry,context_manager,limits): existing request-scoped objects; RunnerLimits stores original tool-call/round limits explicitly. Factories do not open or close shared network clients and must create appropriately bound Runner objects.
- RunnerRequirements(capabilities): comes exclusively from the separately injected application runner_requirements callback, not RouteDecision or model-generated text. Applications must derive these requirements from trusted application/business state; a model's claimed capability or Runner name is not authoritative.
- RoutingPolicy.select(context,definitions,requirements) -> RouteDecision(runner_id,reason): synchronous, replaceable selection. Context and declarations are read-only values. RouteDecision intentionally has no authoritative capability-requirement field.
- RoutingStepRunner captures the trusted capability set before policy invocation, independently validates registration/capability satisfaction, and awaits exactly one selected Runner. No fallback after dispatch starts.

Registered capability declarations express execution support, not permission grants or a security sandbox for arbitrary Python plugins. Existing ToolRegistry/MCP server permissions remain the enforcement boundary. Registry/factories/policy/provider are application-trusted code.

## Default routing and dependency injection

Existing AgentRuntime calls work unchanged. Default registry contains tool_calling with capability tool_calling; no extra model call is made. The selected factory passes the same model/registry/context, limits, require_step_outcome=True and existing default format-repair behavior to ToolCallingStepRunner.

Optional keywords on Runtime and RagAgentService:
- runner_registry: RunnerRegistry
- routing_policy: RoutingPolicy
- runner_requirements: Callable[[StepExecutionContext],RunnerRequirements]

CapabilityRoutingPolicy defaults to tool_calling when requirements are empty. With nonempty requirements it checks configured preferences/default, otherwise a unique matching candidate; no candidate or unresolved ambiguity raises StepRoutingError. Async policies/providers are rejected rather than silently introducing an uncontrolled model wait.

Runtime init and rebind both call _assemble_runner(). Rebind builds everything first, so construction failure leaves the current binding intact. Replacement is rejected while Runtime.run holds its lock or a step is RUNNING. Successful rebind invalidates the old dispatcher and preserves WorkingMemory, step receipts, execution/replan counts and existing format-repair count semantics. Model/MCP lifetimes remain controlled by the existing service context managers.

## State, failure and cancellation

- StepRoutingError subclasses StepRunError. StepExecutor records a failed step and clears its active state through the existing API; Runtime treats it as terminal, without replanning to repair configuration.
- Existing tool/business/model/format/blocked errors retain their handling. Runner exceptions propagate through the dispatcher without trying another Runner.
- Binding invalidation rejects late success and failure. Cancellation requests remain cancellation even if a Runner accidentally suppresses CancelledError then returns/raises; cancelled output cannot commit.
- StepExecutor checks execution ownership for declared failures, cancellation, unexpected exceptions and invalid returned data. WorkingMemory's existing token check still owns successful commits.
- Runtime rejects stale execution without failing newer task state. This HEAD-based service naturally propagates the stale exception. Additional failed-Turn bookkeeping belongs to the separate uncommitted lifecycle feature and is not included in this commit.
- Existing timeout behavior/replan ceilings remain. Cancellation is cooperative: there is no rollback of tool side effects, forced termination guarantee for hostile coroutines, or safety boundary against application code deliberately mutating private internals/clearing cancellation state.

Runtime.routing_decisions returns immutable diagnostic events containing task/step/goal/plan identity, selected Runner, trusted required capabilities and selection reason. Events survive rebind within the live task but are not a new durable execution checkpoint and do not alter StepExecutionReceipt/AgentRunResult/archive schemas.

## Adding a second Runner later

1. Implement async run(context:StepExecutionContext)->StepRunOutput, honoring cancellation and existing success/failure/blocked semantics. Do not mutate WorkingMemory directly; use the common result/commit path and valid resource IDs.
2. Define a stable Runner ID and truthful capability set; register a synchronous factory alongside the default registry:

```python
registry = default_runner_registry()
registry.register(
    RunnerDefinition("specialized", "Application-specific execution", frozenset({"specialized"})),
    lambda deps: MyStepRunner(deps.model, deps.context_manager),
)
```

MyStepRunner is an example name only, not implemented in this phase.

3. Supply an application-owned runner_requirements callback for the appropriate task/step. Do not trust the policy's choice to define its own requirements. Configure preferences or inject a policy when multiple registered Runners can satisfy them.
4. Inject registry/policy/provider into the service or Runtime. Keep model/tool resource opening and cleanup in the composition root; construct fresh bound Runners on resume.
5. Verify trusted constraints, ambiguity/errors, no fallback/repeated side effects, malformed output, stale state, cancellation/timeout, resume and dependency refresh. Run default-parity and existing regression tests before enabling new routing.

No Planner change is required for registration or custom program routing. Planner capability hints/structured Step schema and asynchronous model-based routing are deferred designs, not implicitly enabled features.

## Files

New production:app/agent/runner_registry.py, runner_router.py, routing_step_runner.py.
Modified production:app/agent/runtime.py, step_runner.py, step_executor.py, app/services/rag_agent_service.py.
New tests:tests/agent/test_runner_registry.py, test_runner_routing.py, test_runtime_routing.py. The isolated tree includes41 Runner tests; the full workspace has an additional test of its newer service Turn lifecycle.
Baseline snapshot:tmp/multi-runner-baseline-20261031. Existing user Planner/ToolCallingStepRunner/Memory changes were preserved.

## Verification evidence

- Independent commit tree:539 passed,1 skipped, including41 new Runner tests. Fresh independent review repeated41 tests and found no Critical/Important issues.
- Full working tree with other uncommitted features:1024 passed,2 skipped. Those features/tests are not implicitly included here.
- Real model and real filesystem MCP:all3 final commit-tree scenarios passed (multi-tool write/readback, pause/close/reopen/rebind, actual missing-file Replan). See multi_runner_live_validation.md for initial failure, test-only fail-fast hook and raw report locations.
- Direct-vs-routed default execution asserts identical model-message arguments and StepRunOutput. Existing offline MCP/context tests pass in both trees.
- Source compilation and scoped whitespace checks passed; no model/.env/dependency changes or pushes. The user explicitly authorized the isolated local commit after live validation.

```powershell
.venv/Scripts/python.exe -m pytest tests/agent/test_runner_registry.py tests/agent/test_runner_routing.py tests/agent/test_runtime_routing.py -q --basetemp tmp/pytest-runner-check-UNIQUE
.venv/Scripts/python.exe -m pytest -q --basetemp tmp/pytest-runner-full-UNIQUE
```
