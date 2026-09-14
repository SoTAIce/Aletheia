# WorkingMemory V1

`WorkingMemory` combines one `TaskState` with one `Resources` registry. It does
not inherit `BaseMemory`: task transitions and coordination do not fit the
conversation store's `add` / `retrieve` / `clear` interface. Conversation history
remains in `ConversationMemory`.

```python
from app.models.resources import ResourceType
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory, StepExecutionResult

wm = WorkingMemory(TaskState(user_id="user-1", active_goal="Inspect a report"))
wm.task_state.start_planning()
# An external loader/tool owns the actual content and performs I/O.
resource_id = wm.resources.register("store://report", ResourceType.FILE)
wm.resources.mark_loaded(resource_id, summary="Report overview")
wm.resources.select_context(resource_id, "Relevant excerpt")
# Normally Executor.accept_plan() installs Planner output through this boundary.
wm.install_plan(["Inspect the report"])
token = wm.begin_step(wm.task_state.plan[0].step_id)
wm.commit_step_result(StepExecutionResult(
    token, "Finding supported by the report", "Report loaded", (resource_id,),
))

snapshot = wm.snapshot()
wm.update_goal("Compare the report with last year's findings")
# The resource and its summary remain; its selected context is cleared.
# After a goal change with an existing plan revision, use replan().
wm.install_plan(["Compare reports"])
```

## Coordination rules

- `snapshot()` returns frozen task and resource values without changing state.
- `update_goal()` delegates lifecycle checks to `TaskState`, then clears selected
  contexts only if `goal_revision` changed. Rejected or unchanged goals retain
  the existing selections.
- `record_observation()` and `record_intermediate_result()` accept a tuple of
  resource IDs and return the new record ID. All references are checked before
  writing. Whitespace-padded IDs are stored in canonical form.
- Construction, snapshots and plan installation reject dangling references
  without changing either child. This also detects unsupported direct-child
  edits before inconsistent context reaches the Planner.
- `record_failure()` validates and canonicalizes sources before delegating to
  `TaskState.add_failure()`. Retained failure sources protect resource removal.
- `remove_resource()` protects all retained references: the latest observation,
  intermediate results (including historical/invalid results), and failures.
  It raises `ResourceInUseError` if referenced, otherwise returns the removed
  `ResourceRef`. It never deletes external content.
- `clear_selected_contexts()` returns the number of selections cleared and keeps
  loaded status and summaries.

## Planning validity and versions

| Version | Meaning |
| --- | --- |
| `goal_revision` | Increases only on a changed, normalized goal. |
| `plan_revision` | Increases only on successful plan installation; never resets after a goal change. |
| `state_version` | Increases on each successful task mutation, including plan installation; rejected operations and unchanged goals do not advance it. |
| `Resources.context_version` | Increases on effective selected-context changes, clearing or removal of a selected resource; changing back still advances it. |

Planner proposals carry the task ID, goal revision, base task-state version and
base resource-context version from the input snapshot, not from model output.
Executor verifies these before installation. A separate base plan revision is
unnecessary: every installed plan advances the monotonic task-state version.
The installed revision appears on `PlanAcceptance` and every `PlanStep`.

V1 accepts goal, constraint, pinned-context and question edits only between steps.
A goal change clears the plan and selected resource context. Constraint/pin edits
and adding or answering current-goal questions retain the old plan as evidence but
move the task to PLANNING: neither another step nor task completion is allowed
until a new plan is installed. These edits advance `state_version` once, without
pretending a new goal or plan was installed. Answering a historical question does
not invalidate the current plan, and historical blockers do not block completion.

Ordinary observations and intermediate results are execution evidence, not changes
to requirements; they do not automatically invalidate installed plans. They do
invalidate proposals generated from an older task-state snapshot. Runtime decides
whether new evidence calls for replanning.

Selected context is checked separately at the WorkingMemory boundary. `install_plan()`
binds the new plan to its resource-context version; `begin_step()`,
`commit_step_result()` and `complete_task()` reject an unbound plan or a changed
selection. Replan from fresh context to continue. Raw TaskState plan operations
remain available for standalone use, but integrated callers must install through
Executor/WorkingMemory and use WorkingMemory's execution/completion methods for
resource consistency. Registration and loading alone do not change selected context
and do not invalidate a plan; external file changes are not monitored.

Acceptance and `next_step` are candidate information, not execution authorization.
Blocking questions remain exposed even when a plan is accepted. TaskState permits
explicit clarification steps; the caller must prevent dependent work before a
blocking answer is available. Finish/fail the active step before recording the
answer, then replan. Task completion requires all steps completed and no unresolved
blocking questions for the current goal.

## Execution result submission

Use `begin_step()` to start a pending step and receive an immutable
`ExecutionToken`. A successful runner returns `StepExecutionResult`; submitting
it records an optional observation, appends a reusable result and completes the
step as one task update:

```python
from app.models.working_memory import StepExecutionResult

# After Executor.accept_plan(proposal), using its returned receipt:
token = wm.begin_step(receipt.next_step.step_id)
# The runner performs I/O outside WorkingMemory and registers any output resource.
resource_id = wm.resources.register("tool://output-1", ResourceType.TOOL_RESULT)
result_id = wm.commit_step_result(StepExecutionResult(
    token=token,
    content="Finding supported by the tool output",
    observation="Tool returned the requested data",
    source_refs=(resource_id,),
))
```

The token binds one execution to its workspace, task, goal revision, plan revision,
step and task state version. A successful commit consumes it; repeated or late
submissions raise `StaleExecutionError`. Any task mutation after `begin_step()`
invalidates submission, even if the active step has not changed. Callers must
reconcile stale work explicitly (for example, fail the still-active step and
replan); do not relabel an old result with a new token. An invalid payload or
missing source can be corrected and resubmitted if task state is unchanged.

All source references are checked before task mutation. TaskState stages the
observation, result and completion on a shallow copy whose records are immutable,
then publishes only after every operation succeeds. The successful commit advances
`state_version` once. This is an in-memory, serialized operation, not a database
transaction or a lock for concurrent threads. An omitted observation preserves
the previous one. Source references apply to both the new observation and result.

This API submits successful results only; failures use `WorkingMemory.record_failure()`.
It does not complete the overall task, resolve blockers, or execute tools.
Execution tokens are local to the WorkingMemory instance and are not restored
from snapshots. Resource registrations may happen during execution and do not
invalidate a token. Changing selected context rejects a successful commit; callers
can record the active step's failure and replan explicitly. External content
versioning remains deferred. Direct child mutation bypasses coordination.

## V1 limits and design clarifications

The properties cannot be reassigned, but their child objects remain mutable.
Use the coordinator for plan installation, execution/completion, goal changes,
referenced records, and resource removal;
calling the corresponding child methods directly bypasses its protections.
Give each workspace its own children and serialize access in the caller. This
version does not enforce ownership, provide thread safety, or roll back arbitrary
exceptions from custom child implementations.

Snapshots are immutable captures, not concurrent transactions or persistence
checkpoints. `TaskState.state_version` changes only for task mutations; it cannot
detect resource-only changes and must not be used as a complete workspace
optimistic-lock version.

Changing the goal retains historical scratchpad entries. Consumers must inspect
their `goal_revision`, `plan_revision`, and `valid` fields before treating them as
current evidence. Deletion protection deliberately retains their provenance.

No tools, runtime loop, RAG, database, raw file storage, or automatic conversation
synchronization is included in WorkingMemory. Planner and the proposal-receiving
Executor remain separate modules consuming its snapshots and installation API.

V1 interface changes: `PlannerContext.open_questions` replaces the singular
`open_question`; manually constructed proposals must include
`base_resource_context_version`. Initial plans use `set_plan()` through the
coordinator; `TaskState.replan()` now requires a previously installed plan.

Run the model tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/models -q
```
