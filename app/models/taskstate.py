from dataclasses import dataclass, replace
from copy import copy
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


@dataclass(frozen=True, slots=True)
class TaskStateSnapshot:
    """Read-only task state captured without changing its version or timestamps."""

    task_id: str
    user_id: str
    status: TaskStatus
    active_goal: str
    goal_revision: int
    plan: tuple[PlanStep, ...]
    plan_revision: int
    plan_goal_revision: int | None
    active_step_id: str | None
    state_version: int
    created_at: datetime
    updated_at: datetime
    scratchpad: Scratchpad
    pinned_contexts: tuple[PinnedContext, ...]
    constraints: tuple[Constraint, ...]


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
        self._scratchpad = Scratchpad()
        self._pinned_contexts: tuple[PinnedContext, ...] = ()
        self._constraints: tuple[Constraint, ...] = ()

    def start_planning(self):
        if not self._status == TaskStatus.CREATED:
            raise InvalidStateTransitionError(f"Status Error")
        if self._active_step_id is not None:
            raise  InvalidStateTransitionError(f"Before planning step cannot be active")
        self._status = TaskStatus.PLANNING
        self._updated_at = datetime.now(UTC)
        self._state_version += 1

    def set_plan(self,steps: list[str]):
        if not self._status == TaskStatus.PLANNING:
            raise InvalidStateTransitionError(f"Status Error")
        
        if self._plan_revision != 0:
            raise InvalidStateTransitionError(f"Initial plan has already been set")

        if not isinstance(steps, list):
            raise TypeError("steps must be a list of descriptions")
        if not steps:
            raise ValueError("Plan steps cannot be empty.")
    
        for step in steps:
            if not isinstance(step, str) or not step.strip():
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
                plan_revision = self._plan_revision + 1,
                created_at = now,
            )
            for description in steps
        )
        self._plan = new_plan
        self._plan_revision += 1
        self._plan_goal_revision = self._goal_revision
        self._active_step_id = None
        self._status = TaskStatus.READY
        self._updated_at = now
        self._state_version += 1

    def replan(self, steps: list[str]) -> None:
        """Replace the current plan with a new revision for the active goal."""
        if self._status not in (TaskStatus.PLANNING, TaskStatus.READY):
            raise InvalidStateTransitionError("Task must be planning or ready to replan")
        if self._active_step_id is not None:
            raise InvalidStateTransitionError("Cannot replan while a step is active")
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
        self._state_version += 1

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
        self._status = TaskStatus.READY
        self._updated_at = now
        self._state_version += 1

    def commit_step_result(
        self, step_id: str, expected_state_version: int, content: str,
        observation: str | None = None, source_refs: tuple[str, ...] = (),
    ) -> str:
        """Stage a successful result and publish all task changes together.

        Callers serialize access. WorkingMemory additionally checks execution
        identity and resource existence. No live fields change if staging fails.
        """
        if type(expected_state_version) is not int:
            raise TypeError("expected_state_version must be an integer")
        if expected_state_version != self._state_version:
            raise InvalidStateTransitionError("State changed during execution")
        staged = copy(self)
        if observation is not None:
            staged.set_last_observation(observation, source_refs)
        result_id = staged.add_intermediate_result(content, source_refs)
        staged.complete_step(step_id, result_summary=content)
        self._plan = staged._plan
        self._scratchpad = staged._scratchpad
        self._active_step_id = staged._active_step_id
        self._status = staged._status
        self._updated_at = staged._updated_at
        self._state_version += 1
        return result_id

    def fail(self, step_id: str, message: str) -> str:
        """Compatibility entry point for recording an active step failure."""
        if step_id != self._active_step_id:
            raise InvalidStateTransitionError(f"Step {step_id} is not the active step")
        return self.add_failure(message)

    def update_goal(self, new_goal: str) -> None:
        # 1. 校验新 goal
        if not isinstance(new_goal, str) or not new_goal.strip():
            raise ValueError("new_goal must be a non-empty string")

        new_goal = new_goal.strip()

        # 2. 终态任务不能修改目标
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot update goal for a completed or failed task"
            )

        # 3. 当前有正在执行的 Step，V1 暂时禁止修改目标
        if self._active_step_id is not None:
            raise InvalidStateTransitionError(
                "Cannot update goal while a step is active"
            )

        # 4. Goal 没变化就直接返回
        if new_goal == self._active_goal:
            return

        # 5. 更新 Goal
        self._active_goal = new_goal
        self._goal_revision += 1

        # 6. 当前 Plan 失效
        self._plan = ()
        self._plan_goal_revision = None
        self._active_step_id = None

        # 7. 重新进入规划阶段
        now = datetime.now(timezone.utc)

        self._status = TaskStatus.PLANNING
        self._updated_at = now
        self._state_version += 1

    def add_intermediate_result(self, content: str, source_refs:tuple[str,...] = (),):
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if not isinstance(source_refs, tuple) or not all(
            isinstance(ref, str) for ref in source_refs
        ):
            raise TypeError("source_refs must be a tuple of strings")

        if self._status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        ):
            raise InvalidStateTransitionError()

        now = datetime.now(timezone.utc)
        result = IntermediateResult(
            result_id = str(uuid4()),
            content = content,
            step_id = self._active_step_id,
            goal_revision = self._goal_revision,
            plan_revision = self._plan_revision,
            source_refs = source_refs,
            valid = True,
            created_at = now, 
        )
        self._scratchpad = replace(
            self._scratchpad,
            intermediate_results = (
                *self._scratchpad.intermediate_results,
                result,
            ),
        )
        self._updated_at = now
        self._state_version += 1

        return result.result_id

    def set_last_observation(
        self, content: str, source_refs: tuple[str, ...] = ()
    ) -> str:
        """Replace the latest observation and return its identifier."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if not isinstance(source_refs, tuple) or not all(
            isinstance(ref, str) for ref in source_refs
        ):
            raise TypeError("source_refs must be a tuple of strings")
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot set an observation for a completed or failed task"
            )

        now = datetime.now(timezone.utc)
        observation = Observation(
            observation_id=str(uuid4()),
            content=content,
            step_id=self._active_step_id,
            goal_revision=self._goal_revision,
            plan_revision=self._plan_revision,
            source_refs=source_refs,
            created_at=now,
        )
        self._scratchpad = replace(
            self._scratchpad, last_observation=observation
        )
        self._updated_at = now
        self._state_version += 1
        return observation.observation_id

    def add_open_question(self, content: str, blocking: bool) -> str:
        """Append an unresolved question and return its identifier."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if not isinstance(blocking, bool):
            raise TypeError("blocking must be a bool")
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot add a question to a completed or failed task"
            )

        now = datetime.now(timezone.utc)
        question = OpenQuestion(
            question_id=str(uuid4()),
            content=content,
            step_id=self._active_step_id,
            goal_revision=self._goal_revision,
            blocking=blocking,
            created_at=now,
            answer=None,
            resolved_at=None,
        )
        self._scratchpad = replace(
            self._scratchpad,
            open_questions=(*self._scratchpad.open_questions, question),
        )
        self._updated_at = now
        self._state_version += 1
        return question.question_id

    def resolve_question(self, question_id: str, answer: str):
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("answer must be a non-empty string")

        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot resolve a question for a completed or failed task"
            )

        target_question = next(
            (
                q for q in self._scratchpad.open_questions
                if q.question_id == question_id
            ),
            None,
        )

        if target_question is None:
            raise KeyError(f"Question {question_id} does not exist")

        if target_question.resolved_at is not None:
            raise InvalidStateTransitionError(
                f"Question {question_id} is already resolved"
            )
        now = datetime.now(timezone.utc)
        resolved_question = replace(
            target_question,
            blocking = False,
            answer = answer,
            resolved_at = now,
        )
        self._scratchpad = replace(
            self._scratchpad,
            open_questions = tuple(
                resolved_question if q.question_id == question_id else q
                for q in self._scratchpad.open_questions
            )
        )
        self._updated_at = now
        self._state_version += 1

    def add_failure(self, message: str) -> str:
        """Record the active step's failure and return to planning."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if self._status != TaskStatus.RUNNING:
            raise InvalidStateTransitionError(
                "Task must be running to record a step failure"
            )
        if self._active_step_id is None:
            raise InvalidStateTransitionError("No active step exists")
        target_step = next(
            (step for step in self._plan
             if step.step_id == self._active_step_id),
             None
        )
        if target_step is None:
            raise KeyError(f"Step {self._active_step_id} does not exist")
        if target_step.status != StepStatus.ACTIVE:
            raise InvalidStateTransitionError("The current step is not active")

        now = datetime.now(timezone.utc)
        # First failure is attempt zero; later failures of the same step increment it.
        retry_count = max(
            (record.retry_count for record in self._scratchpad.failure_history
             if record.step_id == target_step.step_id),
            default=-1,
        ) + 1
        failure = FailureRecord(
            failure_id=str(uuid4()),
            message=message,
            step_id=target_step.step_id,
            goal_revision=target_step.goal_revision,
            plan_revision=target_step.plan_revision,
            retry_count=retry_count,
            source_refs=(),
            created_at=now,
        )
        failed_step = replace(
            target_step,
            status = StepStatus.FAILED,
            finished_at = now
        )
        new_plan = tuple(
            failed_step if step.step_id == target_step.step_id else step
            for step in self._plan
        )
        new_scratchpad = replace(
            self._scratchpad,
            failure_history=(*self._scratchpad.failure_history, failure),
        )
        self._plan = new_plan
        self._scratchpad = new_scratchpad
        self._active_step_id = None
        self._status = TaskStatus.PLANNING
        self._updated_at = now
        self._state_version += 1
        return failure.failure_id

    def pin_content(self, content: str, source: str) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")

        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")

        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot pin context for a completed or failed task"
            )

        now = datetime.now(timezone.utc)

        pinned_context = PinnedContext(
            context_id=str(uuid4()),
            content=content,
            source=source,
            created_at=now,
        )

        self._pinned_contexts = (*self._pinned_contexts, pinned_context)
        self._updated_at = now
        self._state_version += 1

        return pinned_context.context_id

    def add_constraint(self, content: str) -> str:
        """Append a task constraint and return its identifier."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot add a constraint to a completed or failed task"
            )

        now = datetime.now(timezone.utc)
        constraint = Constraint(
            constraint_id=str(uuid4()),
            content=content,
            created_at=now,
        )
        self._constraints = (*self._constraints, constraint)
        self._updated_at = now
        self._state_version += 1
        return constraint.constraint_id

    def complete_task(self) -> None:
       if self._status not in (TaskStatus.READY, TaskStatus.RUNNING):
            raise InvalidStateTransitionError(
                "Task must be ready or running to complete"
            )
       if self._active_step_id is not None:
           raise InvalidStateTransitionError(
                "Cannot complete task while a step is active"
            )

       if not self._plan:
           raise InvalidStateTransitionError(
                "Cannot complete task without a plan"
            )

       if self._plan_goal_revision != self._goal_revision:
            raise InvalidStateTransitionError(
                "Current plan does not match the active goal"
            )
       if any(step.status != StepStatus.COMPLETED for step in self._plan):
            raise InvalidStateTransitionError(
                "All plan steps must be completed"
            )

       if any(
            question.blocking and question.resolved_at is None
            for question in self._scratchpad.open_questions
        ):
        raise InvalidStateTransitionError(
                "Cannot complete task with unresolved blocking questions"
            )
       now = datetime.now(timezone.utc)

       self._status = TaskStatus.COMPLETED
       self._updated_at = now
       self._state_version += 1

    def fail_task(self, message: str) -> str:
        """Terminate the task and record its failure without discarding history."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")

        if self._status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidStateTransitionError(
                "Cannot fail a completed or failed task"
            )

        target_step = None
        if self._active_step_id is not None:
            target_step = next(
                (
                    step for step in self._plan
                    if step.step_id == self._active_step_id
                ),
                None,
            )

            if target_step is None:
                raise InvalidStateTransitionError(
                    "Active step does not exist in the current plan"
                )

            if target_step.status != StepStatus.ACTIVE:
                raise InvalidStateTransitionError(
                    "The referenced step is not active"
                )
        if any(
            step.status == StepStatus.ACTIVE
            and step.step_id != self._active_step_id
            for step in self._plan
        ):
            raise InvalidStateTransitionError(
                "An active step does not match the task's active step identifier"
            )

        now = datetime.now(timezone.utc)

        failure = FailureRecord(
            failure_id = str(uuid4()),
            message = message,
            step_id = self._active_step_id,
            goal_revision = self._goal_revision,
            plan_revision = self._plan_revision,
            retry_count = max(
                (record.retry_count for record in self._scratchpad.failure_history
                 if target_step is not None and record.step_id == target_step.step_id),
                default=0,
            ),
            source_refs = (),
            created_at = now,
        )

        updated_steps = []
        for step in self._plan:
            if step.status == StepStatus.ACTIVE:
                step = replace(step, status=StepStatus.FAILED, finished_at=now)
            elif step.status == StepStatus.PENDING:
                step = replace(step, status=StepStatus.CANCELLED)
            updated_steps.append(step)
        new_plan = tuple(updated_steps)

        new_scratchpad = replace(
            self._scratchpad,
            failure_history=(*self._scratchpad.failure_history, failure),
        )
        self._plan = new_plan
        self._scratchpad = new_scratchpad
        self._active_step_id = None
        self._status = TaskStatus.FAILED
        self._updated_at = now
        self._state_version += 1
        return failure.failure_id


        
    
    def snapshot(self) -> TaskStateSnapshot:
        """Capture current values, sharing immutable records and tuples."""
        return TaskStateSnapshot(
            task_id=self._task_id,
            user_id=self._user_id,
            status=self._status,
            active_goal=self._active_goal,
            goal_revision=self._goal_revision,
            plan=self._plan,
            plan_revision=self._plan_revision,
            plan_goal_revision=self._plan_goal_revision,
            active_step_id=self._active_step_id,
            state_version=self._state_version,
            created_at=self._created_at,
            updated_at=self._updated_at,
            scratchpad=self._scratchpad,
            pinned_contexts=self._pinned_contexts,
            constraints=self._constraints,
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

    @property
    def scratchpad(self) -> Scratchpad:
        return self._scratchpad

    @property
    def pinned_contexts(self) -> tuple[PinnedContext, ...]:
        return self._pinned_contexts

    @property
    def constraints(self) -> tuple[Constraint, ...]:
        return self._constraints
