"""Shared helpers for tests of the loop path (stage 4c): no plan executor, no WorkingMemory."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.agent.agent_loop import LOOP_STEP
from app.agent.loop_run import step_context
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import PlanStep, StepStatus


def started_context(memory=None, *, goal='goal', description=LOOP_STEP, pins=(), user='u'):
    """Start one step on a RunMemory and return the context LoopRun would give its runner."""
    memory = memory or RunMemory(RunState(user, goal))
    for content, source in pins:
        memory.task_state.pin_content(content, source)
    now = datetime.now(UTC)
    step = PlanStep(str(uuid4()), description, StepStatus.PENDING, memory.task_state.goal_revision, 1, now)
    memory.task_state.start(step.step_id)
    return step_context(memory, replace(step, status=StepStatus.ACTIVE, started_at=now), 1)


def loop_reply(text, status='succeeded', reason=None):
    """What plan mode's step result {status, content, reason} becomes on the loop: the answer itself,
    or for 'blocked' an ask_user call whose question is the reason (the content was only a note)."""
    from app.agent.step_models import ModelTurn, ToolCall
    if status == 'blocked':
        return ModelTurn(tool_calls=(ToolCall('ask', 'ask_user', {'question': reason}),))
    if status != 'succeeded':
        raise ValueError(f'no loop reply for status {status!r}')
    return ModelTurn(text)
