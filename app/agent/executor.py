"""Receive planner proposals; tool execution is a separate, future stage."""

from dataclasses import dataclass

from app.agent.planner import PlanProposal
from app.models.taskstate import (
    OpenQuestion,
    PlanStep,
)
from app.models.working_memory import WorkingMemory


class StalePlanError(ValueError):
    """A proposal no longer belongs to the current task state."""


@dataclass(frozen=True, slots=True)
class PlanAcceptance:
    """Installed plan receipt, not evidence that any step has executed.

    next_step is only a candidate. Future execution must recheck state and
    resolve blocking questions before starting dependent work.
    """

    task_id: str
    goal_revision: int
    plan_revision: int
    state_version: int
    steps: tuple[PlanStep, ...]
    next_step: PlanStep
    blocking_questions: tuple[OpenQuestion, ...]


class Executor:
    """V1 planner response receiver bound to one working memory.

    The caller starts planning before asking the planner for a proposal.
    Acceptance is synchronous: validate first, then install via TaskState.
    Access must be serialized by the caller, as for WorkingMemory itself.
    """

    def __init__(self, working_memory: WorkingMemory) -> None:
        if not isinstance(working_memory, WorkingMemory):
            raise TypeError("working_memory must be a WorkingMemory")
        self._working_memory = working_memory

    def accept_plan(self, proposal: PlanProposal) -> PlanAcceptance:
        """Install a fresh proposal once, without starting or completing steps."""
        if not isinstance(proposal, PlanProposal):
            raise TypeError("proposal must be a PlanProposal")

        task = self._working_memory.task_state
        if proposal.task_id != task.task_id:
            raise StalePlanError("Proposal belongs to another task")
        for name, expected in (
            ("goal_revision", task.goal_revision),
            ("base_state_version", task.state_version),
            ("base_resource_context_version", self._working_memory.resources.context_version),
        ):
            value = getattr(proposal, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an integer")
            if value != expected:
                raise StalePlanError(f"Proposal {name} does not match current state")

        # Dataclass annotations do not validate manually constructed proposals.
        if not isinstance(proposal.steps, tuple):
            raise TypeError("proposal.steps must be a tuple")
        if not proposal.steps or any(
            not isinstance(step, str) or not step.strip() for step in proposal.steps
        ):
            raise ValueError("proposal.steps must contain non-empty strings")
        steps = [step.strip() for step in proposal.steps]
        if len(set(steps)) != len(steps):
            raise ValueError("proposal.steps must not contain duplicates")

        self._working_memory.install_plan(steps)

        snapshot = task.snapshot()
        return PlanAcceptance(
            task_id=snapshot.task_id,
            goal_revision=snapshot.goal_revision,
            plan_revision=snapshot.plan_revision,
            state_version=snapshot.state_version,
            steps=snapshot.plan,
            next_step=snapshot.plan[0],
            blocking_questions=tuple(
                question for question in snapshot.scratchpad.open_questions
                if question.goal_revision == snapshot.goal_revision
                and question.blocking and question.resolved_at is None
            ),
        )
