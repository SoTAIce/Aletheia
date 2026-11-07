"""The plan's last step is told its result is the final answer and must follow the goal's form (D-037)."""

from unittest.mock import AsyncMock

from app.agent.step_executor import StepExecutor
from app.agent.tool_calling_step_runner import FINAL_STEP_RULES, ToolCallingStepRunner
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.tools.tool_registry import ToolRegistry


def _contexts(steps):
    """Build the context of every step in turn, as the executor would."""
    wm = WorkingMemory(TaskState('user', '实现函数，最终答案只输出完整的 Python 源码。'))
    wm.task_state.start_planning()
    wm.install_plan(steps)
    contexts = []
    for step in wm.task_state.plan:
        wm.begin_step(step.step_id)
        contexts.append(StepExecutor(wm, AsyncMock()).build_context(step.step_id))
        wm.task_state.complete_step(step.step_id, 'done')
    return contexts


def _system(context, **options):
    """Every system message before the conversation starts, joined."""
    runner = ToolCallingStepRunner(AsyncMock(), ToolRegistry(), **options)
    return '\n\n'.join(m.content for m in runner._build_initial_messages(context) if m.role == 'system')


def test_only_the_last_step_is_marked_final():
    contexts = _contexts(['读取规格', '实现并交付源码'])
    assert [c.final_step for c in contexts] == [False, True]
    assert [c.final_step for c in _contexts(['一步完成'])] == [True]


def test_final_step_rules_are_added_only_to_the_last_step():
    first, last = _contexts(['读取规格', '实现并交付源码'])
    for options in ({}, {'require_step_outcome': True}):
        assert FINAL_STEP_RULES not in _system(first, **options)
        system = _system(last, **options)
        assert system.endswith(FINAL_STEP_RULES.strip())
        # After the context, so the last step's request starts like the earlier ones (D-057).
        runner = ToolCallingStepRunner(AsyncMock(), ToolRegistry(), **options)
        firsts, lasts = runner._build_initial_messages(first), runner._build_initial_messages(last)
        assert firsts[0] == lasts[0] and lasts[-1].content == FINAL_STEP_RULES.strip()
    assert 'nothing else' in FINAL_STEP_RULES and 'however long' in FINAL_STEP_RULES


def test_final_step_flag_reaches_the_model_payload():
    import json
    last = _contexts(['交付'])[0]
    runner = ToolCallingStepRunner(AsyncMock(), ToolRegistry())
    assert json.loads(runner._build_initial_messages(last)[1].content)['final_step'] is True
