"""Planner reliability: shape errors share the format repair budget, and one
provider timeout per planning call is retried inside the caller's deadline."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.agent.planner import Planner
from app.agent.tool_calling_model import ModelTimeoutError
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory

GOOD = '{"steps":["inspect"],"reason":null}'


def _memory(method='plan'):
    wm = WorkingMemory(TaskState('user', 'goal'))
    if method == 'replan':
        wm.task_state.start_planning()
        wm.task_state.set_plan(['original'])
    return wm


def _run(planner, method='plan', wm=None):
    wm = wm or _memory(method)
    return getattr(planner, method)(planner.build_context(wm))


SHAPE_CASES = [
    ('["inspect"]', ValueError, '顶层必须是 JSON 对象'),
    ('{"steps":["inspect"]}', ValueError, '恰好包含 steps 和 reason 两个字段'),
    ('{"steps":["inspect"],"reason":null,"extra":1}', ValueError, '恰好包含 steps 和 reason'),
    ('{"steps":"inspect","reason":null}', TypeError, 'steps 必须是字符串数组'),
    ('{"steps":[],"reason":null}', ValueError, 'steps 至少包含 1 个步骤'),
    (json.dumps({'steps': [f's{i}' for i in range(9)], 'reason': None}), ValueError,
     'steps 最多 8 个，当前有 9 个'),
    ('{"steps":["a","a"],"reason":null}', ValueError, '第 2 个步骤与前面的步骤重复，步骤不能重复'),
    ('{"steps":["a","   "],"reason":null}', ValueError, '第 2 个步骤去除首尾空白后为空'),
    (json.dumps({'steps': ['x' * 301], 'reason': None}), ValueError, '第 1 个步骤超过 300 个字符'),
    ('{"steps":["a",3],"reason":null}', TypeError, '第 2 个步骤必须是字符串'),
    ('{"steps":["a"],"reason":5}', TypeError, 'reason 必须是字符串或 null'),
]


@pytest.mark.parametrize('method', ['plan', 'replan'])
@pytest.mark.parametrize('bad, _error, hint', SHAPE_CASES)
async def test_shape_errors_are_repaired_with_a_specific_hint(method, bad, _error, hint):
    model = AsyncMock()
    model.generate.side_effect = [bad, GOOD]
    planner = Planner(model, max_format_repairs=1)
    wm = _memory(method)
    before = wm.snapshot()
    proposal = await _run(planner, method, wm)
    assert proposal.steps == ('inspect',)
    assert model.generate.await_count == 2 and planner.format_repair_count == 1
    repair_prompt = model.generate.call_args.args[0]
    assert hint in repair_prompt
    assert '工具已禁用' in repair_prompt and '不要编造执行结果' in repair_prompt
    assert wm.snapshot() == before


@pytest.mark.parametrize('bad, error, _hint', SHAPE_CASES)
async def test_exhausted_shape_repair_raises_the_original_error_type(bad, error, _hint):
    model = AsyncMock()
    model.generate.return_value = bad
    planner = Planner(model, max_format_repairs=2)
    with pytest.raises(error) as caught:
        await _run(planner)
    assert type(caught.value) is error
    assert model.generate.await_count == 3 and planner.format_repair_count == 2


async def test_syntax_and_shape_errors_share_one_repair_budget():
    model = AsyncMock()
    model.generate.side_effect = ['not json', '{"steps":["a","a"],"reason":null}', GOOD]
    planner = Planner(model, max_format_repairs=1)
    with pytest.raises(ValueError, match='duplicates an earlier step'):
        await _run(planner)
    assert model.generate.await_count == 2 and planner.format_repair_count == 1

    model = AsyncMock()
    model.generate.side_effect = ['not json', '{"steps":["a","a"],"reason":null}', GOOD]
    planner = Planner(model, max_format_repairs=2)
    assert (await _run(planner)).steps == ('inspect',)
    assert model.generate.await_count == 3 and planner.format_repair_count == 2
    assert 'Serialization error' in model.generate.await_args_list[1].args[0]
    assert '步骤不能重复' in model.generate.await_args_list[2].args[0]


async def test_zero_repair_budget_fails_shape_errors_immediately():
    model = AsyncMock()
    model.generate.return_value = '{"steps":["a","a"],"reason":null}'
    planner = Planner(model, max_format_repairs=0)
    with pytest.raises(ValueError):
        await _run(planner)
    model.generate.assert_awaited_once()
    assert planner.format_repair_count == 0


@pytest.mark.parametrize('ambiguous', [
    '{"steps":["a"],"steps":["b"],"reason":null}',
    '{"steps":["a"],"reason":NaN}',
    '{"steps":["a"],"reason":1e999}',
])
async def test_ambiguous_json_is_still_never_repaired(ambiguous):
    model = AsyncMock()
    model.generate.side_effect = [ambiguous, GOOD]
    planner = Planner(model, max_format_repairs=2)
    with pytest.raises(ValueError):
        await _run(planner)
    model.generate.assert_awaited_once()
    assert planner.format_repair_count == 0


async def test_repair_errors_and_hints_never_quote_model_text():
    secret = 'MODEL-ORIGINAL-TEXT'
    model = AsyncMock()
    model.generate.return_value = json.dumps({'steps': [secret, secret], 'reason': None})
    planner = Planner(model, max_format_repairs=1)
    with pytest.raises(ValueError) as caught:
        await _run(planner)
    assert secret not in str(caught.value)
    assert secret not in caught.value.plan_repair_hint
    repair_prompt = model.generate.call_args.args[0]
    instruction = repair_prompt.rsplit('\n', 1)[-1]
    assert secret not in instruction


@pytest.mark.parametrize('method', ['plan', 'replan'])
async def test_one_model_timeout_is_retried(method):
    model = AsyncMock()
    model.generate.side_effect = [ModelTimeoutError('model request timed out'), GOOD]
    planner = Planner(model)
    proposal = await _run(planner, method)
    assert proposal.steps == ('inspect',)
    assert model.generate.await_count == 2 and planner.timeout_retry_count == 1
    first, second = model.generate.await_args_list
    assert first.args == second.args
    assert planner.format_repair_count == 0


async def test_second_timeout_is_raised_and_budget_is_per_planning_call():
    model = AsyncMock()
    model.generate.side_effect = ModelTimeoutError('model request timed out')
    planner = Planner(model)
    with pytest.raises(ModelTimeoutError):
        await _run(planner)
    assert model.generate.await_count == 2 and planner.timeout_retry_count == 1

    # A timeout during a later repair call has no retry left in the same plan().
    model = AsyncMock()
    model.generate.side_effect = [ModelTimeoutError('t'), 'not json', ModelTimeoutError('t'), GOOD]
    planner = Planner(model)
    with pytest.raises(ModelTimeoutError):
        await _run(planner)
    assert model.generate.await_count == 3

    # Each new plan()/replan() call gets its own single retry.
    model = AsyncMock()
    model.generate.side_effect = [ModelTimeoutError('t'), GOOD, ModelTimeoutError('t'), GOOD]
    planner = Planner(model)
    await _run(planner)
    await _run(planner)
    assert model.generate.await_count == 4 and planner.timeout_retry_count == 2


async def test_other_model_errors_are_not_retried():
    model = AsyncMock()
    model.generate.side_effect = TimeoutError('model timed out')
    planner = Planner(model)
    with pytest.raises(TimeoutError):
        await _run(planner)
    model.generate.assert_awaited_once()
    assert planner.timeout_retry_count == 0


async def test_retry_does_not_outlive_the_outer_planning_deadline():
    calls = 0

    async def generate(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelTimeoutError('model request timed out')
        await asyncio.sleep(10)
        return GOOD

    model = AsyncMock()
    model.generate.side_effect = generate
    planner = Planner(model)
    context = planner.build_context(_memory())
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await planner.plan(context)
    assert loop.time() - started < 2
    assert calls == 2


async def test_no_retry_once_the_outer_deadline_is_cancelling():
    calls = 0

    async def generate(_prompt):
        nonlocal calls
        calls += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            # A provider adapter that maps cancellation to its own timeout.
            raise ModelTimeoutError('model request timed out')
        return GOOD

    model = AsyncMock()
    model.generate.side_effect = generate
    planner = Planner(model)
    context = planner.build_context(_memory())
    with pytest.raises((TimeoutError, ModelTimeoutError)):
        async with asyncio.timeout(0.05):
            await planner.plan(context)
    assert calls == 1 and planner.timeout_retry_count == 0
