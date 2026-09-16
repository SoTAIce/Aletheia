from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.step_executor import StepExecutor
from app.agent.step_models import StepRunOutput, ToolCall, ToolResult
from app.agent.step_runner import StepRunError, ToolRoundLimitError
from app.models.taskstate import StepStatus, TaskStatus
from app.models.working_memory import StaleExecutionError
import asyncio
from app.models.resources import ResourceType
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory


def executor():
    wm = WorkingMemory(TaskState('user', 'goal'))
    return StepExecutor(wm, AsyncMock()), wm


def test_validate_output_accepts_evidence_without_mutation():
    ex, wm = executor()
    rid = wm.resources.register('report', ResourceType.FILE)
    output = StepRunOutput(
        ' conclusion ', observation='observation', source_refs=(rid,),
        tool_calls=(ToolCall('call', 'read', {'path': 'report'}),),
        tool_results=(ToolResult('call', 'read', False, '', (rid,), 'not readable'),),
    )
    before = wm.snapshot()
    normalized = ex._validate_runner_output(output)
    assert normalized == replace(output, content='conclusion')
    assert normalized is not output
    assert output.content == ' conclusion '
    assert wm.snapshot() == before
    assert ex._validate_runner_output(StepRunOutput('no tools')) == StepRunOutput('no tools')


def test_normalized_output_preserves_raw_tool_data_and_is_idempotent():
    ex, wm = executor()
    rid = wm.resources.register('report', ResourceType.FILE)
    call = ToolCall(' call ', ' read ', {'path': ' report '})
    result = ToolResult(' call ', ' read ', False, ' raw\n', (f' {rid} ',), ' error ')
    output = StepRunOutput(' done ', ' observed ', (f' {rid} ',), (call,), (result,))
    normalized = ex._validate_runner_output(output)
    assert normalized.content == 'done'
    assert normalized.observation == 'observed'
    assert normalized.source_refs == (rid,)
    assert normalized.tool_calls[0].call_id == normalized.tool_results[0].call_id == 'call'
    assert normalized.tool_calls[0].tool_name == normalized.tool_results[0].tool_name == 'read'
    assert normalized.tool_calls[0].arguments == {'path': ' report '}
    assert normalized.tool_results[0].content == ' raw\n'
    assert normalized.tool_results[0].error == 'error'
    assert normalized.tool_results[0].source_refs == (rid,)
    assert ex._validate_runner_output(normalized) == normalized
    assert output.source_refs == (f' {rid} ',)
    assert call.call_id == ' call ' and result.error == ' error '


@pytest.mark.parametrize('changes,error', [
    ({'content': None}, TypeError), ({'content': '  '}, ValueError),
    ({'observation': 1}, TypeError), ({'observation': ''}, ValueError),
    ({'source_refs': []}, TypeError), ({'source_refs': (None,)}, TypeError),
    ({'source_refs': (' ',)}, ValueError), ({'source_refs': ('missing',)}, KeyError),
    ({'tool_calls': []}, TypeError), ({'tool_calls': (None,)}, TypeError),
    ({'tool_calls': (ToolCall('', 'read', {}),)}, ValueError),
    ({'tool_calls': (ToolCall('c', 'read', []),)}, TypeError),
    ({'tool_calls': (ToolCall('c', 'read', {1: 'x'}),)}, TypeError),
    ({'tool_results': []}, TypeError), ({'tool_results': (None,)}, TypeError),
    ({'tool_results': (ToolResult('c', '', True, ''),)}, ValueError),
    ({'tool_results': (ToolResult('c', 'read', 1, ''),)}, TypeError),
    ({'tool_results': (ToolResult('c', 'read', True, None),)}, TypeError),
    ({'tool_results': (ToolResult('c', 'read', True, '', ('missing',)),)}, KeyError),
    ({'tool_results': (ToolResult('c', 'read', False, '', error=1),)}, TypeError),
])
def test_validate_output_rejects_invalid_data_without_mutation(changes, error):
    ex, wm = executor()
    before = wm.snapshot()
    with pytest.raises(error):
        ex._validate_runner_output(replace(StepRunOutput('result'), **changes))
    assert wm.snapshot() == before


def test_validate_output_rejects_wrong_object():
    ex, _ = executor()
    with pytest.raises(TypeError, match='StepRunOutput'):
        ex._validate_runner_output(None)


@pytest.mark.asyncio
async def test_execute_commits_normalized_output_and_returns_receipt():
    wm = WorkingMemory(TaskState('user', 'goal'))
    rid = wm.resources.register('report', ResourceType.FILE)
    wm.task_state.start_planning()
    wm.install_plan(['inspect'])
    step_id = wm.task_state.plan[0].step_id
    runner = AsyncMock()
    runner.run.return_value = StepRunOutput(
        ' conclusion ', ' observed ', (f' {rid} ',),
        (ToolCall(' call ', ' read ', {}),),
        (ToolResult(' call ', ' read ', True, 'raw', (f' {rid} ',)),),
    )
    receipt = await StepExecutor(wm, runner).execute(f' {step_id} ')
    assert receipt.step_id == step_id
    assert receipt.task_id == wm.task_state.task_id
    assert receipt.state_version == wm.task_state.state_version
    assert receipt.goal_revision == wm.task_state.goal_revision
    assert receipt.plan_revision == wm.task_state.plan_revision
    assert receipt.result_summary == 'conclusion'
    assert receipt.source_refs == (rid,)
    assert receipt.tool_calls[0].call_id == receipt.tool_results[0].call_id == 'call'
    result = wm.task_state.scratchpad.intermediate_results[-1]
    assert receipt.result_id == result.result_id
    assert result.content == 'conclusion'
    assert wm.task_state.scratchpad.last_observation.content == 'observed'
    assert wm.task_state.active_step_id is None
    runner.run.assert_awaited_once()


def ready_executor():
    wm = WorkingMemory(TaskState('user', 'goal'))
    wm.task_state.start_planning()
    wm.install_plan(['inspect'])
    runner = AsyncMock()
    return StepExecutor(wm, runner), wm, runner, wm.task_state.plan[0].step_id


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [StepRunError, ToolRoundLimitError])
async def test_declared_failure_is_recorded_and_reraised(error_type):
    ex, wm, runner, step_id = ready_executor()
    rid = wm.resources.register('failure-log', ResourceType.TOOL_RESULT)
    error = error_type('tool failed', (f' {rid} ',))
    runner.run.side_effect = error
    with pytest.raises(error_type) as caught:
        await ex.execute(step_id)
    assert caught.value is error
    task = wm.task_state
    assert task.status is TaskStatus.PLANNING
    assert task.active_step_id is None
    assert task.plan[0].status is StepStatus.FAILED
    assert task.scratchpad.intermediate_results == ()
    failure, = task.scratchpad.failure_history
    assert failure.step_id == step_id
    assert failure.source_refs == (rid,)
    assert failure.message == 'tool failed'
    wm.install_plan(['retry'])
    runner.run.side_effect = None
    runner.run.return_value = StepRunOutput('success')
    await ex.execute(wm.task_state.plan[0].step_id)


@pytest.mark.asyncio
async def test_stale_failure_does_not_fail_new_execution():
    ex, wm, runner, step_id = ready_executor()
    error = StepRunError('old failure')
    snapshots = []

    async def change_task(context):
        wm.record_failure('previous execution stopped')
        wm.install_plan(['new step'])
        wm.begin_step(wm.task_state.plan[0].step_id)
        snapshots.append(wm.snapshot())
        raise error

    runner.run.side_effect = change_task
    with pytest.raises(StaleExecutionError) as caught:
        await ex.execute(step_id)
    assert caught.value.__cause__ is error
    assert wm.snapshot() == snapshots[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [StepRunError('failed', ('missing',)),
                                  StepRunError(' '), RuntimeError('bug'),
                                  asyncio.CancelledError()])
async def test_unrecordable_errors_do_not_publish_failure_or_success(error):
    ex, wm, runner, step_id = ready_executor()
    snapshots = []

    async def fail(context):
        snapshots.append(wm.snapshot())
        raise error

    runner.run.side_effect = fail
    expected = (KeyError, ValueError) if isinstance(error, StepRunError) else type(error)
    with pytest.raises(expected):
        await ex.execute(step_id)
    assert wm.snapshot() == snapshots[0]
