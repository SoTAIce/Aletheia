# WorkingMemory V1

`WorkingMemory` combines one `TaskState` with one `Resources` registry. It does
not inherit `BaseMemory`: task transitions and coordination do not fit the
conversation store's `add` / `retrieve` / `clear` interface. Conversation history
remains in `ConversationMemory`.

```python
from app.models.resources import ResourceType
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory

wm = WorkingMemory(TaskState(user_id="user-1", active_goal="Inspect a report"))
wm.task_state.start_planning()
# A future Planner generates these descriptions from wm.snapshot().
wm.task_state.set_plan(["Inspect the report"])
step_id = wm.task_state.plan[0].step_id
wm.task_state.start_step(step_id)

# An external loader/tool owns the actual content and performs I/O.
resource_id = wm.resources.register("store://report", ResourceType.FILE)
wm.resources.mark_loaded(resource_id, summary="Report overview")
wm.resources.select_context(resource_id, "Relevant excerpt")
wm.record_observation("Report loaded", source_refs=(resource_id,))
wm.record_intermediate_result("Finding supported by the report", (resource_id,))
wm.task_state.complete_step(step_id)

snapshot = wm.snapshot()
wm.update_goal("Compare the report with last year's findings")
# The resource and its summary remain; its selected context is cleared.
# After a goal change with an existing plan revision, use replan().
wm.task_state.replan(["Compare reports"])
```

## Coordination rules

- `snapshot()` returns frozen task and resource values without changing state.
- `update_goal()` delegates lifecycle checks to `TaskState`, then clears selected
  contexts only if `goal_revision` changed. Rejected or unchanged goals retain
  the existing selections.
- `record_observation()` and `record_intermediate_result()` accept a tuple of
  resource IDs and return the new record ID. All references are checked before
  writing. Whitespace-padded IDs are stored in canonical form.
- Construction with existing children rejects dangling references without
  changing either child.
- `remove_resource()` protects all retained references: the latest observation,
  intermediate results (including historical/invalid results), and failures.
  It raises `ResourceInUseError` if referenced, otherwise returns the removed
  `ResourceRef`. It never deletes external content.
- `clear_selected_contexts()` returns the number of selections cleared and keeps
  loaded status and summaries.

## V1 limits and design clarifications

The properties cannot be reassigned, but their child objects remain mutable.
Use the coordinator for goal changes, referenced records, and resource removal;
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

No LLM, tools, RAG, prompt assembly, database, raw file storage, or automatic
conversation synchronization is included. Planner and Executor integration is a
later layer that can consume `WorkingMemorySnapshot`.

Run the model tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/models -q
```
