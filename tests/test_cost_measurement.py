"""Cost measurement (docs/cost_measurement_plan.md): usage booked by purpose, calls outside the
agent model, and how the command line and the bench report them.

Offline only: scripted models and stand-in usage objects.
"""

from types import SimpleNamespace

import pytest

from app.agent import run_meter
from app.agent.hooks import HookSet, HookedModel, current_purpose, model_purpose, using_hooks
from app.agent.run_meter import LEDGER, RunMeter, metering, record_auxiliary, record_model_call, usage_tokens
from app.agent.step_models import ModelTurn, ToolCall, ToolsShownOnly
from app.models.model_profile import ModelProfile


@pytest.fixture(autouse=True)
def empty_ledger():
    LEDGER.clear()
    yield
    LEDGER.clear()


def _profile(name, input_price=None, output_price=None):
    return ModelProfile(name=name, base_url='https://x.test', api_key_env='X_KEY', model=name,
                        context_tokens=8000, max_output_tokens=500, timeout_seconds=30,
                        input_price_per_million=input_price, output_price_per_million=output_price)


def _usage(prompt, completion, cached=None):
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details)


# ---- the meter --------------------------------------------------------------------------------

def test_calls_are_booked_by_the_purpose_they_ran_under():
    main, cheap = _profile('main', 2.0, 8.0), _profile('cheap', 1.0, 4.0)
    meter = RunMeter()
    meter.record(_usage(1_000_000, 100_000, 400_000), profile=main)
    with model_purpose('route'):
        meter.record(_usage(500_000, 50_000), profile=cheap)
        meter.record(None, profile=cheap)                      # no usage: counted, no tokens
    meter.record(_usage(10, 1), profile=main, purpose='lead')   # an explicit purpose wins
    usage = meter.snapshot()
    assert usage['by_purpose'] == {
        'step': {'model_calls': 1, 'prompt_tokens': 1_000_000, 'completion_tokens': 100_000,
                 'cached_prompt_tokens': 400_000, 'estimated_cost': 2.8},
        'route': {'model_calls': 2, 'prompt_tokens': 500_000, 'completion_tokens': 50_000,
                  'cached_prompt_tokens': 0, 'estimated_cost': 0.7},
        'lead': {'model_calls': 1, 'prompt_tokens': 10, 'completion_tokens': 1,
                 'cached_prompt_tokens': 0, 'estimated_cost': 0.000028}}
    assert [c['purpose'] for c in usage['calls']] == ['step', 'route', 'route', 'lead']
    # The totals are what they were before purposes existed.
    assert usage['model_calls'] == 4 and usage['estimated_cost'] == round(2.8 + 0.7 + 0.000028, 6)


def test_an_unpriced_call_makes_its_purpose_cost_unknown():
    meter = RunMeter()
    meter.record(_usage(10, 10), profile=_profile('priced', 1.0, 1.0))
    meter.record(_usage(10, 10), profile=_profile('free'), purpose='member')
    by_purpose = meter.snapshot()['by_purpose']
    assert by_purpose['step']['estimated_cost'] == 0.00002 and by_purpose['member']['estimated_cost'] is None


def test_record_model_call_uses_the_current_purpose():
    meter = RunMeter()
    with metering(meter), model_purpose('explorer'):
        record_model_call(_usage(3, 2))
    assert list(meter.snapshot()['by_purpose']) == ['explorer']


async def test_the_hooked_model_books_a_wrap_up_call_as_wrap_up():
    meter = RunMeter()

    class Model:
        async def generate(self, messages, tools):
            record_model_call(_usage(5, 1))
            return ModelTurn('done')
    with metering(meter), using_hooks(HookSet()):
        await HookedModel(Model()).generate((), ())
        await HookedModel(Model()).generate((), ToolsShownOnly(()))
    assert [c['purpose'] for c in meter.snapshot()['calls']] == ['step', 'wrap_up']
    assert current_purpose() == 'step'


# ---- the team's roles ---------------------------------------------------------------------------

class Booking:
    """A scripted model that books each call, as the real adapter does."""

    def __init__(self, *items):
        self.items = list(items)

    async def generate(self, messages, tools):
        record_model_call(_usage(100, 10))
        return self.items.pop(0)


async def test_the_lead_and_the_members_are_booked_apart():
    from app.agent.delegation import DelegatingLoopRunner
    from app.agent.step_runner import RoleModel
    from test_delegation import SANDBOX, _registry, delegate
    from test_sandbox_step1 import _context
    meter = RunMeter()
    runner = DelegatingLoopRunner(Booking(delegate('d'), ModelTurn('answer')), _registry(), SANDBOX,
                                  member=RoleModel(Booking(ModelTurn('member notes'))))
    with metering(meter):
        await runner.run(_context())
    by_purpose = meter.snapshot()['by_purpose']
    assert by_purpose['lead']['model_calls'] == 2 and by_purpose['member']['model_calls'] == 1
    assert 'step' not in by_purpose


async def test_explore_members_are_booked_as_explorers():
    from app.agent.delegation import DELEGATE, ExploreLoopRunner
    from app.agent.step_models import ToolDefinition, ToolOutput
    from app.agent.step_runner import RoleModel
    from app.tools.tool_registry import ToolRegistry
    from loop_fakes import started_context

    async def read(arguments):
        return ToolOutput(True, 'x = 1')
    registry = ToolRegistry()
    registry.register(SimpleNamespace(definition=ToolDefinition('read_file', 'read', {'type': 'object'}), invoke=read))
    main = Booking(ModelTurn(tool_calls=(ToolCall('a', 'read_file', {'path': 'a.py'}),)),
                   ModelTurn(tool_calls=(ToolCall('d', DELEGATE, {'task': 'Find x.', 'thoroughness': 'quick'}),)),
                   ModelTurn('final answer'))
    meter = RunMeter()
    with metering(meter):
        await ExploreLoopRunner(main, registry, member=RoleModel(Booking(ModelTurn('in a.py')))).run(
            started_context(goal='explain', user='user'))
    by_purpose = meter.snapshot()['by_purpose']
    assert by_purpose['step']['model_calls'] == 3 and by_purpose['explorer']['model_calls'] == 1


def test_a_reviewer_is_booked_as_reviewer():
    import asyncio
    from app.agent.delegation import MemberSpawner
    seen = []

    class Runner:
        async def run(self, context):
            seen.append(current_purpose())
    asyncio.run(MemberSpawner.run(Runner(), None, 'reviewer-1', 'reviewer'))
    assert seen == ['reviewer']


# ---- calls outside the agent model ---------------------------------------------------------------

def test_outside_calls_go_to_the_request_or_to_the_ledger():
    meter = RunMeter()
    with metering(meter):
        record_auxiliary('embedding', 12)
        record_auxiliary('query_rewrite')                          # nothing reported
        record_auxiliary('summary', 300, 80, in_request=False)    # background: never the request's
    usage = meter.snapshot()
    assert usage['auxiliary'] == {
        'embedding': {'calls': 1, 'calls_without_usage': 0, 'prompt_tokens': 12, 'completion_tokens': 0},
        'query_rewrite': {'calls': 1, 'calls_without_usage': 1, 'prompt_tokens': 0, 'completion_tokens': 0}}
    # Never part of the request's model calls, tokens or budget.
    assert usage['model_calls'] == 0 and usage['total_tokens'] == 0 and 'by_purpose' not in usage
    record_auxiliary('embedding', 5)                               # no request open
    assert LEDGER.snapshot() == {
        'summary': {'calls': 1, 'calls_without_usage': 0, 'prompt_tokens': 300, 'completion_tokens': 80},
        'embedding': {'calls': 1, 'calls_without_usage': 0, 'prompt_tokens': 5, 'completion_tokens': 0}}


def test_usage_tokens_reads_the_shapes_providers_use():
    assert usage_tokens(SimpleNamespace(prompt_tokens=7, completion_tokens=2)) == (7, 2)
    assert usage_tokens({'input_tokens': 4, 'output_tokens': 1}) == (4, 1)
    assert usage_tokens(SimpleNamespace(prompt_tokens=None, total_tokens=9)) == (9, None)
    assert usage_tokens(None) == (None, None) and usage_tokens({'prompt_tokens': -1}) == (None, None)


def test_embeddings_book_their_tokens():
    from app.services.vector_embedding_service import DashScopeEmbeddings

    class Client:
        class embeddings:
            @staticmethod
            def create(**kwargs):
                count = len(kwargs['input']) if isinstance(kwargs['input'], list) else 1
                return SimpleNamespace(data=[SimpleNamespace(embedding=[0.0] * 4)] * count,
                                       usage=SimpleNamespace(prompt_tokens=6, total_tokens=6))
    embeddings = DashScopeEmbeddings('sk-test-key-123456', dimensions=4, client=Client())
    meter = RunMeter()
    with metering(meter):
        embeddings.embed_query('hello')
        embeddings.embed_documents(['a', 'b'])
    assert meter.snapshot()['auxiliary']['embedding'] == {
        'calls': 2, 'calls_without_usage': 0, 'prompt_tokens': 12, 'completion_tokens': 0}


async def test_summaries_are_booked_to_the_ledger_even_inside_a_request():
    from datetime import datetime, timezone
    from app.models.conversation_memory import ConversationMessage, ConversationTurn, MessageRole
    from app.services.memory_compression.llm_compressor import SUMMARY_HEADINGS, LLMCompressor
    message = SimpleNamespace(role='assistant', content='\n'.join(SUMMARY_HEADINGS), refusal=None,
                              tool_calls=None, function_call=None)

    class Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=message)],
                                   usage=SimpleNamespace(prompt_tokens=300, completion_tokens=80))
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    stamp = datetime.now(timezone.utc)
    turn = ConversationTurn([ConversationMessage(MessageRole.USER, 'question', stamp)], 't1', stamp)
    meter = RunMeter()
    with metering(meter):          # a background summary may start while a request is open
        await LLMCompressor(client, 'summary-model').compress(None, [turn])
    assert 'auxiliary' not in meter.snapshot()
    assert LEDGER.snapshot() == {'summary': {'calls': 1, 'calls_without_usage': 0,
                                             'prompt_tokens': 300, 'completion_tokens': 80}}


async def test_query_rewrites_book_their_usage():
    from app.schemas.rag import QueryRewriteResult
    from app.services.rag_query_rewrite_service import QueryRewriteService, _UsageCatcher

    class Structured:
        async def ainvoke(self, prompt, config=None):
            (catcher,) = config['callbacks']
            message = SimpleNamespace(usage_metadata={'input_tokens': 40, 'output_tokens': 9})
            catcher.on_llm_end(SimpleNamespace(llm_output=None, generations=[[SimpleNamespace(message=message)]]))
            return QueryRewriteResult(rewritten_queries=['better query'], reason='r')

    class LLM:
        def with_structured_output(self, schema):
            return Structured()
    meter = RunMeter()
    with metering(meter):
        result = await QueryRewriteService(LLM()).rewrite('what is it')
    assert result.rewritten_queries == ['better query']
    assert meter.snapshot()['auxiliary']['query_rewrite'] == {
        'calls': 1, 'calls_without_usage': 0, 'prompt_tokens': 40, 'completion_tokens': 9}
    broken = _UsageCatcher()
    broken.on_llm_end(object())                      # unreadable usage never fails the call
    assert (broken.prompt, broken.completion) == (None, None)


# ---- the command line -----------------------------------------------------------------------------

def test_the_command_line_shows_purposes_only_when_there_is_more_than_one():
    from app.agent.__main__ import purpose_lines
    one = {'by_purpose': {'step': {'model_calls': 3, 'prompt_tokens': 900, 'completion_tokens': 100,
                                   'estimated_cost': 0.002}}}
    assert purpose_lines(one) == []
    team = {'by_purpose': {'lead': {'model_calls': 2, 'prompt_tokens': 1000, 'completion_tokens': 200,
                                    'estimated_cost': 0.0042},
                           'member': {'model_calls': 5, 'prompt_tokens': 8000, 'completion_tokens': 500,
                                      'estimated_cost': None}},
            'auxiliary': {'embedding': {'calls': 2, 'prompt_tokens': 30, 'completion_tokens': 0},
                          'query_rewrite': {'calls': 1, 'prompt_tokens': 0, 'completion_tokens': 0}}}
    assert purpose_lines(team) == [
        '按用途：团队负责人 2 次 1,200 令牌（估算费用 0.0042）；团队成员 5 次 8,500 令牌',
        '其他调用（不计入预算）：嵌入 2 次 30 令牌；查询改写 1 次（没有报告用量）']
    assert purpose_lines(None) == []


def test_the_status_line_stays_last():
    from app.agent.__main__ import report
    from app.models.taskstate import TaskStatus
    lines = []
    usage = {'by_purpose': {'step': {'model_calls': 1}, 'route': {'model_calls': 1}}}
    report(SimpleNamespace(content='answer', user_message=None, status=TaskStatus.COMPLETED,
                           execution_count=1, replan_count=0, usage=usage), lines.append)
    assert lines[-2].startswith('按用途：') and lines[-1].startswith('\nStatus:')


def test_the_result_file_carries_the_calls_outside_any_request():
    from app.agent.__main__ import outside_line, result_record
    from app.models.taskstate import TaskStatus
    result = SimpleNamespace(content='a', user_message=None, status=TaskStatus.COMPLETED, failure_kind=None,
                             execution_count=1, replan_count=0, usage={}, step_log=())
    assert 'auxiliary_outside_requests' not in result_record(result) and outside_line() is None
    record_auxiliary('summary', 100, 20, in_request=False)
    assert result_record(result)['auxiliary_outside_requests']['summary']['calls'] == 1
    assert outside_line() == '任务之外的调用（后台摘要、记忆写入等）：历史摘要 1 次 120 令牌'


# ---- the bench -----------------------------------------------------------------------------------

def test_bench_rows_keep_purposes_and_the_summary_adds_them_up():
    from bench.run_bench import purpose_headline, purpose_summary, usage_fields
    record = {'usage': {'calls': [{'prompt': 5, 'completion': 1, 'purpose': 'route'}],
                        'by_purpose': {'step': {'model_calls': 3, 'prompt_tokens': 600, 'completion_tokens': 100,
                                                'cached_prompt_tokens': 200, 'estimated_cost': 0.003},
                                       'route': {'model_calls': 1, 'prompt_tokens': 90, 'completion_tokens': 10,
                                                 'cached_prompt_tokens': 0, 'estimated_cost': 0.0001}},
                        'auxiliary': {'embedding': {'calls': 1, 'calls_without_usage': 0,
                                                    'prompt_tokens': 8, 'completion_tokens': 0}}},
              'auxiliary_outside_requests': {'summary': {'calls': 1, 'calls_without_usage': 0,
                                                         'prompt_tokens': 50, 'completion_tokens': 5}}}
    row = usage_fields(record)
    assert row['calls'] == [{'prompt': 5, 'completion': 1, 'purpose': 'route'}]
    assert set(row['by_purpose']) == {'step', 'route'} and 'embedding' in row['auxiliary']
    second = {'by_purpose': {'step': {'model_calls': 1, 'prompt_tokens': 200, 'completion_tokens': 0,
                                      'estimated_cost': None}}}
    table = purpose_summary([row, row])
    assert table['tokens'] == 1600 and table['model']['step']['token_share'] == 0.875
    assert table['model']['route']['cost_share'] == round(0.0002 / 0.0062, 3)
    assert table['other'] == {'embedding': {'calls': 2, 'calls_without_usage': 0, 'tokens': 16},
                              'summary': {'calls': 2, 'calls_without_usage': 0, 'tokens': 110}}
    lines = purpose_headline(table)
    assert lines[0].startswith('按用途（共 1,600 令牌）：主循环 6 次，令牌 88%，费用 97%')
    assert lines[1] == '其他调用：嵌入 2 次 16 令牌；历史摘要 2 次 110 令牌'
    unpriced = purpose_summary([row, second])
    assert unpriced['estimated_cost'] is None and unpriced['model']['step']['cost_share'] is None
    assert purpose_summary([]) == {'model': {}, 'other': {}, 'tokens': 0, 'estimated_cost': 0}
