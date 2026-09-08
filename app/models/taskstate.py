from dataclasses import dataclass, replace
from datetime import UTC, datetime, timezone
from enum import Enum
from uuid import uuid4
import uuid
class InvalidStateTransitionError(Exception):
    pass


class TaskStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    description: str
    status: StepStatus
    goal_revision: int
    plan_revision: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_summary: str | None = None


class TaskState:
    """Mutable task aggregate with externally read-only state."""

    def __init__(self, user_id: str, active_goal: str) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must not be empty")
        if not isinstance(active_goal, str) or not active_goal.strip():
            raise ValueError("active_goal must not be empty")

        now = datetime.now(UTC)
        self._task_id = str(uuid4())
        self._user_id = user_id
        self._status = TaskStatus.CREATED
        self._active_goal = active_goal
        self._goal_revision = 1
        self._plan: tuple[PlanStep, ...] = ()
        self._plan_revision = 0
        self._plan_goal_revision: int | None = None
        self._active_step_id: str | None = None
        self._state_version = 0
        self._created_at = now
        self._updated_at = now

    def start_planning(self):
        if not self._status == TaskStatus.CREATED:
            raise InvalidStateTransitionError(f"Status Error")
        if self._active_step_id is not None:
            raise  InvalidStateTransitionError(f"Before planning step cannot be active")
        self._status = TaskStatus.PLANNING
        self._state_version += 1

    def set_plan(self,steps: list[str]):
        if not self._status == TaskStatus.PLANNING:
            raise InvalidStateTransitionError(f"Status Error")
        
        if self._plan_revision != 0:
            raise InvalidStateTransitionError(f"Initial plan has already been set")

        if not steps:
            raise ValueError("Plan steps cannot be empty.")
    
        for step in steps:
            if not isinstance(step, str) and not step.strip():
                raise ValueError(
                "Each plan step must contain a non-empty description."
            )
        now = datetime.now(timezone.utc)
        new_plan = tuple(
            PlanStep(
                step_id = str(uuid4()),
                description = description,
                status = StepStatus.PENDING,
                goal_revision = self._goal_revision, 
                plan_revision = self._plan_revision,
                created_at = now,
            )
            for description in steps
        )
        self._plan = new_plan
        self._plan_goal_revision = self._goal_revision
        self._active_step_id = None
        self._status = TaskStatus.READY

    def replan(self, steps: list[str]) -> None:
        """Replace the current plan with a new revision for the active goal."""
        if not isinstance(steps, list):
            raise TypeError("steps must be a list of descriptions")
        if not steps:
            raise ValueError("Plan steps cannot be empty.")
        for description in steps:
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Each plan step must contain a non-empty description.")

        now = datetime.now(timezone.utc)
        new_revision = self._plan_revision + 1
        new_plan = tuple(
            PlanStep(
                step_id=str(uuid4()),
                description=description,
                status=StepStatus.PENDING,
                goal_revision=self._goal_revision,
                plan_revision=new_revision,
                created_at=now,
            )
            for description in steps
        )

        self._plan = new_plan
        self._plan_revision = new_revision
        self._plan_goal_revision = self._goal_revision
        self._active_step_id = None
        self._status = TaskStatus.READY
        self._updated_at = now
        self._state_version += 1

    def start_step(self, step_id: str):
        
        if self._status not in (TaskStatus.READY, TaskStatus.RUNNING):
            raise InvalidStateTransitionError("Status Error")
        if self._active_step_id is not None:
            raise InvalidStateTransitionError(
                f"Step {self._active_step_id} is already active"
            )
        target_step = None

        for step in self._plan:
            if step.step_id == step_id:
                target_step = step
                break

        if target_step is None:
             raise KeyError(f"Step {step_id} does not exist in current plan")

        if target_step.status != StepStatus.PENDING:
            raise InvalidStateTransitionError(
                f"Step {step_id} is not pending"
            )
        next_step = next(
            (step for step in self._plan if step.status == StepStatus.PENDING),
            None,
        )
        if next_step is None:
            raise InvalidStateTransitionError(
                "No pending step exists in current plan"
            )

        if next_step.step_id != step_id:
            raise InvalidStateTransitionError(
                f"Step {step_id} is not the next pending step"
            )

        now = datetime.now(timezone.utc)

        active_step = replace(
            target_step,
            status = StepStatus.ACTIVE,
            started_at = now,
        )
        self._plan = tuple(
            active_step if step.step_id == step_id else step
            for step in self._plan
        )
        self._active_step_id = step_id
        self._status = TaskStatus.RUNNING

        self._updated_at = now

    def complete_step(
        self, step_id: str, result_summary: str | None = None
    ) -> None:
        if self._status != TaskStatus.RUNNING:
            raise InvalidStateTransitionError("Task must be running to complete a step")
        if self._active_step_id is None:
            raise InvalidStateTransitionError("No step is currently active")

        target_step = next(
            (step for step in self._plan if step.step_id == step_id),
            None,
        )
        if target_step is None:
            raise KeyError(f"Step {step_id} does not exist in current plan")
        if self._active_step_id != step_id:
            raise InvalidStateTransitionError(f"Step {step_id} is not the active step")
        if target_step.status != StepStatus.ACTIVE:
            raise InvalidStateTransitionError(f"Step {step_id} is not active")
        if result_summary is not None and not isinstance(result_summary, str):
            raise TypeError("result_summary must be a string or None")

        now = datetime.now(timezone.utc)
        completed_step = replace(
            target_step,
            status=StepStatus.COMPLETED,
            finished_at=now,
            result_summary=result_summary,
        )
        self._plan = tuple(
            completed_step if step.step_id == step_id else step
            for step in self._plan
        )
        self._active_step_id = None
        self._status = (
            TaskStatus.COMPLETED
            if all(step.status == StepStatus.COMPLETED for step in self._plan)
            else TaskStatus.READY
        )
        self._updated_at = now
        self._state_version += 1

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
    def plan(self) -> tuple[PlanStep, ...]:
        return self._plan

    @property
    def plan_revision(self) -> int:
        return self._plan_revision

    @property
    def plan_goal_revision(self) -> int | None:
        return self._plan_goal_revision

    @property
    def active_step_id(self) -> str | None:
        return self._active_step_id

    @property
    def state_version(self) -> int:
        return self._state_version

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at
