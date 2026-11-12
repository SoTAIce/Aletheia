"""Value types of a task run: statuses, the step record, results, questions, context and constraints.

RunState (app/models/run_state.py) owns them. Until stage 4c this module also held TaskState, the plan
and step state machine of plan mode; it went with plan mode (D-076).
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
class InvalidStateTransitionError(Exception):
    pass


class TaskStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class StepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


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

@dataclass(frozen = True, slots = True)
class IntermediateResult:
    result_id: str
    content: str

    step_id: str | None

    goal_revision: int
    plan_revision: int

    source_refs: tuple[str, ...] = ()

    valid: bool = True

    created_at: datetime | None = None

@dataclass(frozen = True, slots = True)
class Observation:
    observation_id: str
    content: str
    step_id: str | None

    goal_revision: int
    plan_revision: int

    source_refs: tuple[str, ...]
    created_at: datetime

@dataclass(frozen = True, slots = True)
class OpenQuestion:
    question_id: str
    content: str

    step_id: str | None
    goal_revision: int

    blocking: bool

    created_at: datetime
    answer: str | None = None
    resolved_at: datetime | None = None

@dataclass(frozen = True, slots = True)
class FailureRecord:
    failure_id: str
    message: str

    step_id: str | None

    goal_revision: int
    plan_revision: int

    retry_count: int

    source_refs: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Scratchpad:
    intermediate_results: tuple[IntermediateResult, ...] = ()
    last_observation: Observation | None = None
    open_questions: tuple[OpenQuestion, ...] = ()
    failure_history: tuple[FailureRecord, ...] = ()

@dataclass(frozen=True, slots=True)
class PinnedContext:
    context_id: str
    content: str
    source: str
    created_at: datetime

@dataclass(frozen=True, slots=True)
class Constraint:
    """A task-wide constraint retained across goal and plan changes."""

    constraint_id: str
    content: str
    created_at: datetime
