"""Task-scoped coordination of task state and external resource references."""

from dataclasses import dataclass
from collections.abc import Iterator
from uuid import uuid4

from .resources import ResourceRef, Resources
from .taskstate import TaskState, TaskStateSnapshot


class ResourceInUseError(RuntimeError):
    """A retained task record still references a resource being removed."""


class StaleExecutionError(ValueError):
    """An execution has expired, already committed, or belongs elsewhere."""


@dataclass(frozen=True, slots=True)
class ExecutionToken:
    execution_id: str
    task_id: str
    goal_revision: int
    plan_revision: int
    step_id: str
    state_version: int


@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    """Successful runner output; source_refs apply to both evidence fields."""

    token: ExecutionToken
    content: str
    observation: str | None = None
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkingMemorySnapshot:
    task_state: TaskStateSnapshot
    resources: tuple[ResourceRef, ...]


class WorkingMemory:
    """A single-task, in-memory workspace with no I/O or planning logic.

    Callers own serialization of access and must not share the mutable children
    across workspaces. Use this object's methods for goal changes, referenced
    records and resource removal; direct child calls bypass coordination.
    TaskState.state_version tracks task mutations, not resource mutations.
    """

    def __init__(
        self, task_state: TaskState, resources: Resources | None = None
    ) -> None:
        if not isinstance(task_state, TaskState):
            raise TypeError("task_state must be a TaskState")
        if resources is not None and not isinstance(resources, Resources):
            raise TypeError("resources must be Resources or None")
        self._task_state = task_state
        self._resources = resources if resources is not None else Resources()
        self._execution_token: ExecutionToken | None = None
        # Reject pre-existing dangling references without modifying either child.
        for refs in self._record_source_refs():
            self._validate_source_refs(refs)

    @property
    def task_state(self) -> TaskState:
        """The task's public API; the property itself cannot be reassigned."""
        return self._task_state

    @property
    def resources(self) -> Resources:
        """The resource API; use remove_resource for coordinated deletion."""
        return self._resources

    def snapshot(self) -> WorkingMemorySnapshot:
        """Capture immutable values without changing versions or timestamps."""
        return WorkingMemorySnapshot(
            task_state=self._task_state.snapshot(),
            resources=tuple(self._resources.list_all()),
        )

    def begin_step(self, step_id: str) -> ExecutionToken:
        """Start the next pending step and issue one token for its result."""
        execution_id = str(uuid4())
        task = self._task_state
        task.start_step(step_id)
        token = ExecutionToken(
            execution_id=execution_id, task_id=task.task_id,
            goal_revision=task.goal_revision, plan_revision=task.plan_revision,
            step_id=task.active_step_id, state_version=task.state_version,
        )
        self._execution_token = token
        return token

    def commit_step_result(self, result: StepExecutionResult) -> str:
        """Validate then publish evidence and completion; return the result ID.

        Rejected results change neither memory nor the issued token. A successful
        commit consumes the token. No tool I/O occurs here.
        """
        if not isinstance(result, StepExecutionResult):
            raise TypeError("result must be a StepExecutionResult")
        token = result.token
        if not isinstance(token, ExecutionToken):
            raise TypeError("result.token must be an ExecutionToken")
        for name in ("goal_revision", "plan_revision", "state_version"):
            if type(getattr(token, name)) is not int:
                raise TypeError(f"token.{name} must be an integer")
        task = self._task_state
        if (
            token != self._execution_token
            or token.task_id != task.task_id
            or token.goal_revision != task.goal_revision
            or token.plan_revision != task.plan_revision
            or token.step_id != task.active_step_id
            or token.state_version != task.state_version
        ):
            raise StaleExecutionError("Execution is no longer current")
        refs = self._validate_source_refs(result.source_refs)
        result_id = task.commit_step_result(
            token.step_id, token.state_version, result.content,
            observation=result.observation, source_refs=refs,
        )
        self._execution_token = None
        return result_id

    def update_goal(self, new_goal: str) -> None:
        """Clear selected context only after a successful goal revision."""
        previous_revision = self._task_state.goal_revision
        self._task_state.update_goal(new_goal)
        if self._task_state.goal_revision != previous_revision:
            self.clear_selected_contexts()

    def record_observation(
        self, content: str, source_refs: tuple[str, ...] = ()
    ) -> str:
        """Validate resource IDs before replacing the latest observation."""
        refs = self._validate_source_refs(source_refs)
        return self._task_state.set_last_observation(content, source_refs=refs)

    def record_intermediate_result(
        self, content: str, source_refs: tuple[str, ...] = ()
    ) -> str:
        """Validate resource IDs before appending a reusable result."""
        refs = self._validate_source_refs(source_refs)
        return self._task_state.add_intermediate_result(content, source_refs=refs)

    def remove_resource(self, resource_id: str) -> ResourceRef:
        """Remove an index entry only if no retained record references it.

        Historical and invalid results, the last observation and failures all
        protect references. This operation never deletes external content.
        """
        resource = self._resources.get(resource_id)
        for refs in self._record_source_refs():
            if resource.resource_id in (ref.strip() for ref in refs):
                raise ResourceInUseError(
                    f"Resource {resource.resource_id} is referenced by task records"
                )
        return self._resources.remove(resource.resource_id)

    def clear_selected_contexts(self) -> int:
        """Clear the current selection and return the number of changed resources."""
        return self._resources.clear_all_selected_contexts()

    def _validate_source_refs(self, source_refs: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(source_refs, tuple) or not all(
            isinstance(ref, str) for ref in source_refs
        ):
            raise TypeError("source_refs must be a tuple of strings")
        # Store canonical IDs, not the whitespace-padded input accepted by get().
        return tuple(self._resources.get(ref).resource_id for ref in source_refs)

    def _record_source_refs(self) -> Iterator[tuple[str, ...]]:
        scratchpad = self._task_state.scratchpad
        if scratchpad.last_observation is not None:
            yield scratchpad.last_observation.source_refs
        for result in scratchpad.intermediate_results:
            yield result.source_refs
        for failure in scratchpad.failure_history:
            yield failure.source_refs
