"""Authoritative state for a single loop run, independent of plan execution."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from app.models.resources import Resources
from app.models.taskstate import (
    Constraint,
    FailureRecord,
    InvalidStateTransitionError,
    OpenQuestion,
    Observation,
    PinnedContext,
    Scratchpad,
    TaskStatus,
)


@dataclass(frozen=True, slots=True)
class RunStateSnapshot:
    """Immutable view captured without changing the run's version or timestamps."""

    task_id: str
    user_id: str
    status: TaskStatus
    active_goal: str
    goal_revision: int
    state_version: int
    created_at: datetime
    updated_at: datetime
    scratchpad: Scratchpad
    pinned_contexts: tuple[PinnedContext, ...]
    constraints: tuple[Constraint, ...]
    active_step_id: str | None


class RunState:
    """Mutable loop aggregate with externally read-only state."""

    def __init__(self, user_id: str, active_goal: str) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not isinstance(active_goal, str) or not active_goal.strip():
            raise ValueError("active_goal must not be empty")

        now = datetime.now(UTC)
        self._task_id = str(uuid4())
        self._user_id = user_id
        self._status = TaskStatus.CREATED
        self._active_goal = active_goal.strip()
        self._goal_revision = 1
        self._state_version = 0
        self._created_at = now
        self._updated_at = now
        self._scratchpad = Scratchpad()
        self._pinned_contexts: tuple[PinnedContext, ...] = ()
        self._constraints: tuple[Constraint, ...] = ()
        self._active_step_id: str | None = None

    def _check_planning_change(self) -> None:
        if self._active_step_id is not None:
            raise InvalidStateTransitionError(
                "Cannot change planning inputs while a step is active"
            )

    def _touch(self, now: datetime) -> None:
        self._updated_at = now
        self._state_version += 1

    def pin_content(self, content: str, source: str) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot pin context for a completed or failed task"
            )
        self._check_planning_change()
        now = datetime.now(UTC)
        context = PinnedContext(str(uuid4()), content, source, now)
        self._pinned_contexts = (*self._pinned_contexts, context)
        self._touch(now)
        return context.context_id

    def add_constraint(self, content: str) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot add a constraint to a completed or failed task"
            )
        self._check_planning_change()
        now = datetime.now(UTC)
        constraint = Constraint(str(uuid4()), content, now)
        self._constraints = (*self._constraints, constraint)
        self._touch(now)
        return constraint.constraint_id

    def start(self, step_id: str) -> None:
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("step_id must be a non-empty string")
        if self._status not in (TaskStatus.CREATED, TaskStatus.READY):
            raise InvalidStateTransitionError("Task must be created or ready to start")
        self._active_step_id = step_id
        self._status = TaskStatus.RUNNING
        self._touch(datetime.now(UTC))

    def block(self, question: str, step_id: str, *, plan_revision: int = 0,
              source_refs: tuple[str, ...] = ()) -> str:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if self._status is not TaskStatus.RUNNING:
            raise InvalidStateTransitionError("Task must be running to block")
        if step_id != self._active_step_id:
            raise InvalidStateTransitionError(f"Step {step_id} is not the active step")
        if not isinstance(source_refs, tuple) or not all(isinstance(ref, str) for ref in source_refs):
            raise TypeError("source_refs must be a tuple of strings")
        now = datetime.now(UTC)
        record = OpenQuestion(str(uuid4()), question.strip(), step_id, 1, True, now)
        self._scratchpad = replace(
            self._scratchpad,
            open_questions=(*self._scratchpad.open_questions, record),
            last_observation=Observation(
                str(uuid4()), question.strip(), step_id, self._goal_revision,
                plan_revision, source_refs, now,
            ),
        )
        self._active_step_id = None
        self._status = TaskStatus.BLOCKED
        self._touch(now)
        return record.question_id

    def resolve_question(self, question_id: str, answer: str) -> None:
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("answer must be a non-empty string")
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot resolve a question for a completed or failed task"
            )
        target = next(
            (q for q in self._scratchpad.open_questions if q.question_id == question_id),
            None,
        )
        if target is None:
            raise KeyError(f"Question {question_id} does not exist")
        if target.resolved_at is not None:
            raise InvalidStateTransitionError(f"Question {question_id} is already resolved")
        self._check_planning_change()
        now = datetime.now(UTC)
        resolved = replace(target, blocking=False, answer=answer, resolved_at=now)
        self._scratchpad = replace(
            self._scratchpad,
            open_questions=tuple(
                resolved if q.question_id == question_id else q
                for q in self._scratchpad.open_questions
            ),
        )
        if self._status is TaskStatus.BLOCKED and not self._has_blocking_questions():
            self._status = TaskStatus.READY
        self._touch(now)

    def _has_blocking_questions(self) -> bool:
        return any(q.blocking and q.resolved_at is None
                   for q in self._scratchpad.open_questions)

    def add_failure(self, message: str, source_refs: tuple[str, ...] = ()) -> str:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if not isinstance(source_refs, tuple) or not all(
            isinstance(ref, str) for ref in source_refs
        ):
            raise TypeError("source_refs must be a tuple of strings")
        if self._status is not TaskStatus.RUNNING:
            raise InvalidStateTransitionError("Task must be running to record a step failure")
        now = datetime.now(UTC)
        record = FailureRecord(
            failure_id=str(uuid4()), message=message, step_id=self._active_step_id,
            goal_revision=1, plan_revision=0, retry_count=0,
            source_refs=source_refs, created_at=now,
        )
        self._scratchpad = replace(
            self._scratchpad,
            failure_history=(*self._scratchpad.failure_history, record),
        )
        self._active_step_id = None
        self._status = TaskStatus.READY
        self._touch(now)
        return record.failure_id

    def complete(self) -> None:
        if self._status is not TaskStatus.RUNNING:
            raise InvalidStateTransitionError("Task must be running to complete")
        if self._has_blocking_questions():
            raise InvalidStateTransitionError(
                "Cannot complete task with unresolved blocking questions"
            )
        self._active_step_id = None
        self._status = TaskStatus.COMPLETED
        self._touch(datetime.now(UTC))

    def fail_task(self, message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError("Cannot fail a completed or failed task")
        now = datetime.now(UTC)
        record = FailureRecord(
            failure_id=str(uuid4()), message=message, step_id=self._active_step_id,
            goal_revision=1, plan_revision=0, retry_count=0,
            source_refs=(), created_at=now,
        )
        self._scratchpad = replace(
            self._scratchpad,
            failure_history=(*self._scratchpad.failure_history, record),
        )
        self._active_step_id = None
        self._status = TaskStatus.FAILED
        self._touch(now)
        return record.failure_id

    def snapshot(self) -> RunStateSnapshot:
        return RunStateSnapshot(
            task_id=self._task_id, user_id=self._user_id, status=self._status,
            active_goal=self._active_goal, goal_revision=self._goal_revision,
            state_version=self._state_version, created_at=self._created_at,
            updated_at=self._updated_at, scratchpad=self._scratchpad,
            pinned_contexts=self._pinned_contexts, constraints=self._constraints,
            active_step_id=self._active_step_id,
        )

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def status(self) -> TaskStatus:
        return self._status

    @property
    def active_goal(self) -> str:
        return self._active_goal

    @property
    def goal_revision(self) -> int:
        return self._goal_revision

    @property
    def state_version(self) -> int:
        return self._state_version

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def scratchpad(self) -> Scratchpad:
        return self._scratchpad

    @property
    def pinned_contexts(self) -> tuple[PinnedContext, ...]:
        return self._pinned_contexts

    @property
    def constraints(self) -> tuple[Constraint, ...]:
        return self._constraints

    @property
    def active_step_id(self) -> str | None:
        return self._active_step_id


class RunMemory:
    """Loop state and resources supplied to the run's tools."""

    def __init__(self, task_state: RunState, resources: Resources | None = None) -> None:
        if not isinstance(task_state, RunState):
            raise TypeError("task_state must be a RunState")
        self._task_state = task_state
        self._resources = resources if resources is not None else Resources()

    @property
    def task_state(self) -> RunState:
        return self._task_state

    @property
    def resources(self) -> Resources:
        return self._resources
