"""Real Session/Coordinator recovery with controlled provider responses."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.team.coordinator import TeamCoordinator, REVIEW
from app.agent.team.models import TeamEnvironment, TeamOptions, TeamStore, TeamError
from app.tools.tool_registry import ToolRegistry
from test_runner_routing import context
from test_team_session_poc import ScriptedModel, ReadTool, factory, call
from test_team_v1 import action


def truncate():
    # The real adapter rejects the response BEFORE any tool is returned/executed.
    model = OpenAICompatibleToolCallingModel(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda: None))),
        'model', max_output_tokens=128)
    model._parse_response(SimpleNamespace(choices=[SimpleNamespace(finish_reason='length')],
        usage=SimpleNamespace(prompt_tokens=500, completion_tokens=128)))


async def truncated(messages):
    truncate()


def setup_model(*reviewer_turns):
    return ScriptedModel(
        action('l1', 'create_worker', 'writer', instruction='read', tools='read'),
        action('l2', 'run_member', 'writer'), call('read-w'),
        call('draft', 'submit_result', payload={'answer': '73'}),
        action('l3', 'create_reviewer', 'checker', instruction='verify', tools='read'),
        action('l4', 'review', 'checker', 'writer', version=1),
        *reviewer_turns,
        action('l5', 'finish', 'writer', version=1))


async def test_reviewer_output_limit_repairs_submission_only_and_retains_reads():
    read, owners = ReadTool(), []
    @asynccontextmanager
    async def tools(memory, manager):
        async with factory(read, owners)() as registry:
            yield registry
    model = setup_model(call('read-r'), truncated,
        call('r', 'submit_result', payload={'candidate_version': 1, 'decision': 'approve', 'feedback': 'verified 73'}))
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions(allowed_tools=frozenset({'read'})))
    output = await TeamCoordinator(state, model, TeamEnvironment('u', tools, lambda m: ()), ctx).run()
    assert json.loads(output.content) == {'answer': '73'}
    reviewer = state.members['checker'].session
    assert reviewer.format_repairs == 1
    assert reviewer.request_tool_names[-1] == ('submit_result',)
    assert state.budget.tool_calls == 2
    assert [r.tool_name for r in reviewer.tool_results] == ['read', 'submit_result']
    limit = json.loads(next(e.detail for e in reviewer.events if e.kind == 'output_limit'))
    assert limit['finish_reason'] == 'length' and limit['completion_tokens'] == 128
    assert len(owners) == 4


async def test_reviewer_feedback_is_bounded_in_schema_and_strict_validation():
    schema = REVIEW.schema()
    assert schema['properties']['feedback']['maxLength'] == 2000
    from app.agent.team.contracts import ContractError
    with pytest.raises(ContractError):
        REVIEW.serialize({'candidate_version': 1, 'decision': 'approve', 'feedback': 'x' * 2001})


async def test_output_limit_recovery_is_finite_and_nested_failure_is_recorded_safely(caplog):
    @asynccontextmanager
    async def tools(memory, manager):
        try:
            async with factory(ReadTool())() as registry:
                yield registry
        except Exception as exc:
            raise ExceptionGroup('PRIVATE MCP teardown', [exc, RuntimeError('PRIVATE API KEY')]) from None
    model = setup_model(truncated, truncated, truncated)
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions(allowed_tools=frozenset({'read'})))
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, TeamEnvironment('u', tools, lambda m: ()), ctx).run()
    assert state.status == 'failed' and state.output is None
    reviewer = state.members['checker'].session
    assert reviewer.model_calls == 3 and reviewer.format_repairs == 2
    failure = json.dumps(state.failure)
    assert 'ModelOutputLimitError' in failure and 'RuntimeError' in failure
    assert 'length' in failure and 'PRIVATE' not in failure
    assert 'ModelOutputLimitError' in json.dumps(reviewer.failure)
    assert 'ModelOutputLimitError' in caplog.text and 'PRIVATE' not in caplog.text
    assert 'completion_tokens' in caplog.text
    assert reviewer.failure['session_id'] == 'checker'


async def test_non_truncation_member_failure_is_not_retried():
    async def fail(messages):
        raise RuntimeError('PRIVATE provider response')
    model = setup_model(fail)
    # Worker needs a real read tool, Reviewer model fails before using any tools.
    @asynccontextmanager
    async def source(memory, manager):
        async with factory(ReadTool())() as registry:
            yield registry
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions(allowed_tools=frozenset({'read'})))
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, TeamEnvironment('u', source, lambda m: ()), ctx).run()
    assert state.members['checker'].session.model_calls == 1
    assert state.members['checker'].session.format_repairs == 0
    assert 'PRIVATE' not in json.dumps(state.failure)


async def test_submission_repair_cannot_invoke_business_tools():
    @asynccontextmanager
    async def tools(memory, manager):
        async with factory(ReadTool())() as registry:
            yield registry
    model = setup_model(call('read-r'), truncated, call('illegal-repeat'))
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions(allowed_tools=frozenset({'read'})))
    with pytest.raises(TeamError):
        await TeamCoordinator(state, model, TeamEnvironment('u', tools, lambda m: ()), ctx).run()
    assert state.status == 'failed' and state.budget.tool_calls == 2
    assert [r.call_id for r in state.members['checker'].session.tool_results] == ['read-r']


async def test_worker_output_limit_remains_terminal_without_automatic_retry():
    @asynccontextmanager
    async def tools(memory, manager):
        yield ToolRegistry()
    model = ScriptedModel(action('create', 'create_worker', 'writer', instruction='draft'),
        action('run', 'run_member', 'writer'), truncated)
    ctx = context()
    state = TeamStore().get(ctx, TeamOptions())
    with pytest.raises(TeamError, match='ModelOutputLimitError'):
        await TeamCoordinator(state, model, TeamEnvironment('u', tools, lambda m: ()), ctx).run()
    assert state.members['writer'].session.model_calls == 1
    assert state.members['writer'].session.format_repairs == 0
