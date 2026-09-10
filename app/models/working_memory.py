"""Task-scoped coordination of task state and external resource references."""

from dataclasses import dataclass
from collections.abc import Iterator

from .resources import ResourceRef, Resources
from .taskstate import TaskState, TaskStateSnapshot


class ResourceInUseError(RuntimeError):
    """A retained task record still references a resource being removed."""


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
