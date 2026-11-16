"""Stage 5c: splitting ContextManager changes no request (docs/stage5_plan.md).

tests/fixtures/context_split_golden.json was recorded from the code before the split (commit 4c8d6f0):
every request a loop sends while its history is compacted, the retry after a context overflow, the
reductions of prepare(), the projection and archiving of tool results, and request counts. The file
holds expected values, not snapshots to refresh: a difference is a behaviour change to explain.
"""

import json
import pathlib
import re

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_models import (AssistantMessage, ModelTurn, SystemMessage, ToolCall, ToolDefinition,
                                   ToolMessage, ToolOutput, ToolResult, UserMessage)
from app.agent.tool_calling_model import ModelContextOverflowError
from app.services.context_manager import ContextManager

GOLDEN = pathlib.Path(__file__).parent / 'fixtures' / 'context_split_golden.json'
_UUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')
_TIME = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:\+00:00|Z)?')


def _wire(messages):
    text = json.dumps(OpenAICompatibleToolCallingModel._build_message_payload(tuple(messages)), ensure_ascii=False)
    return _TIME.sub('<time>', _UUID.sub('<uuid>', text))


class Recorder:
    def __init__(self, *items):
        self.items, self.requests = list(items), []

    async def generate(self, messages, tools):
        self.requests.append(_wire(messages))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Tool:
    def __init__(self, name, content):
        self.definition = ToolDefinition(name, name, {'type': 'object'})
        self.content = content

    async def invoke(self, arguments):
        return ToolOutput(True, self.content)


def _call(identity, name, **arguments):
    return ModelTurn(tool_calls=(ToolCall(identity, name, arguments),))


async def _loop_requests():
    """A loop whose history outgrows 60 % and then 85 % of a small window."""
    from app.agent.agent_loop import AgentLoopRunner, LOOP_STEP, LoopOptions
    from app.agent.loop_run import LoopRun
    from app.models.run_state import RunMemory, RunState
    from app.tools.tool_registry import ToolRegistry
    registry = ToolRegistry()
    for name, content in (('read_file', 'r' * 2500), ('write_file', 'File written successfully.'),
                          ('run_python', 'o' * 1500)):
        registry.register(Tool(name, content))
    turns = [_call('w1', 'write_file', path='a.py', content='a' * 3000),
             _call('w2', 'write_file', path='b.py', content='b' * 3000)]
    turns += [_call(f'r{i}', 'read_file', path=f'f{i}.py') for i in range(8)]
    turns += [_call('w3', 'write_file', path='a.py', content='c' * 3000),
              _call('p1', 'run_python', code='p' * 2500), _call('p2', 'run_python', code='q' * 2500),
              _call('w4', 'write_file', path='c.py', content='d' * 3000), ModelTurn('done')]
    model = Recorder(*turns)
    manager = ContextManager(window_tokens=9000, output_tokens=300, safety_tokens=100)
    run = LoopRun(RunMemory(RunState('user', 'rewrite the module')), model, registry,
                  runner_factory=lambda d: AgentLoopRunner(d.model, d.tool_registry, LoopOptions(),
                                                           d.context_manager),
                  step_description=LOOP_STEP, context_manager=manager)
    result = await run.run()
    return {'status': result.status.value, 'failure_kind': result.failure_kind, 'requests': model.requests}


def _pinned_messages():
    history = {'summary': None,
               'turns': [{'turn_id': f't{i}', 'user': 'u' * 400, 'assistant': 'a' * 400} for i in range(6)],
               'summary_index': [{'memory_id': 'm1', 'source_turn_ids': ['t0']}],
               'summary_bodies': [{'memory_id': 'm1', 'content': 's' * 600}],
               'raw_index': [{'turn_id': 'x', 'snippet': 'z' * 200}]}
    memories = {'policy': 'p', 'entries': [{'memory_id': f'l{i}', 'content': 'l' * 300} for i in range(4)]}
    document = {'active_goal': 'answer', 'pinned_contexts': [
        {'source': 'conversation_memory', 'content': json.dumps(history)},
        {'source': 'longterm_memory', 'content': json.dumps(memories)}]}
    tool = json.dumps({'call_id': 'c1', 'tool_name': 'read_file', 'success': True, 'content': 'k' * 1200,
                       'error': None, 'source_refs': [], 'storage_ref': 'ref-1', 'truncated': True})
    return (SystemMessage('system'), UserMessage('Instructions first.\n' + json.dumps(document) + '\nrepair'),
            AssistantMessage(None, (ToolCall('c1', 'read_file', {'path': 'a.py'}),)),
            ToolMessage('c1', tool), ToolMessage('c2', 'not json'))


async def _overflow_requests():
    manager = ContextManager(window_tokens=20000, output_tokens=300, safety_tokens=100)
    model = Recorder(ModelContextOverflowError('too long'), ModelTurn('ok'))
    turn = await manager.generate(model, _pinned_messages(), ())
    return {'content': turn.content, 'requests': model.requests,
            'interrupted': manager.overflow_recovery_interrupted}


def _prepare_cases():
    found = {}
    for window in (6000, 4000, 2500, 1200, 600):
        manager = ContextManager(window_tokens=window, output_tokens=300, safety_tokens=100)
        try:
            found[str(window)] = _wire(manager.prepare(_pinned_messages(), ()))
        except Exception as exc:          # ContextBudgetError when nothing more can go
            found[str(window)] = type(exc).__name__
    return found


async def _projections():
    saved = []

    class Archive:
        def save_text(self, text, *, kind, metadata):
            saved.append({'chars': len(text), 'kind': kind, 'metadata': metadata})
            return f'ref-{len(saved)}'

    manager = ContextManager(window_tokens=32768, output_tokens=2048, safety_tokens=1024, archive=Archive())
    results = [ToolResult('a', 'read_file', True, 'x' * 12000),
               ToolResult('b', 'run_python', False, 'y' * 6000, error='e' * 6000),
               ToolResult('c', 'read_context_artifact', True, 'z' * 12000),
               ToolResult('d', 'read_file', True, 'small'),
               ToolResult('e', 'read_file', True, 'w' * 9000),
               ToolResult('a', 'read_file', True, 'x' * 12000)]
    views = [await manager.project_tool_result(r) for r in results]
    return {'views': views, 'saved': saved,
            'budgets': [list(ContextManager.tool_budgets(w, 2048, 1024)) for w in (8192, 32768, 128000, 1000000)]}


def _counts():
    manager = ContextManager()
    tools = (ToolDefinition('read_file', 'Read one file.', {'type': 'object', 'properties': {'path': {}}}),)
    return [manager.count_request(_pinned_messages(), tools), manager.count_request((UserMessage('hi'),), ())]


async def _everything():
    return {'loop': await _loop_requests(), 'overflow': await _overflow_requests(),
            'prepare': _prepare_cases(), 'projections': await _projections(), 'counts': _counts()}


async def test_the_split_sends_the_requests_recorded_before_it():
    found = json.loads(json.dumps(await _everything(), ensure_ascii=False))
    expected = json.loads(GOLDEN.read_text(encoding='utf-8'))
    assert found['loop']['status'] == expected['loop']['status']
    assert len(found['loop']['requests']) == len(expected['loop']['requests'])
    for index, (new, old) in enumerate(zip(found['loop']['requests'], expected['loop']['requests'])):
        assert new == old, f'loop request {index + 1} differs'
    assert found == expected


def test_the_parts_share_one_budget_and_the_facade_forwards_to_them():
    manager = ContextManager(window_tokens=9000, output_tokens=300, safety_tokens=100, tool_result_chars=50)
    assert manager.conversation.budget is manager.tool_results.budget is manager.fitter.budget is manager.budget
    manager.window_tokens = 8000                     # an assignment reaches every part, as before
    assert manager.fitter.window_tokens == 8000 and manager.budget.input_tokens == 7600
    assert manager.tool_result_chars == 50 and manager.tool_results.tool_result_chars == 50
    manager.overflow_recovery_interrupted = True
    assert manager.fitter.overflow_recovery_interrupted is True
    assert ContextManager.tool_budgets(32768, 2048, 1024) == (9898, 1414)


def test_the_budget_keeps_its_old_validation_messages():
    import pytest
    from app.services.context_budget import ContextBudget
    for kwargs, message in (({'window_tokens': 0}, 'window_tokens must be an integer >= 1'),
                            ({'output_tokens': True}, 'output_tokens must be an integer >= 1'),
                            ({'window_tokens': 100, 'output_tokens': 90, 'safety_tokens': 10},
                             'Output reserve and safety must be smaller than window_tokens')):
        with pytest.raises(ValueError, match=message):
            ContextBudget(**kwargs)
        with pytest.raises(ValueError, match=message):
            ContextManager(**kwargs)


async def test_only_the_loops_compact_the_history():
    # Direct callers of generate() (knowledge tool, query rewrite) never had the loop's compaction.
    messages = (UserMessage('goal'),
                AssistantMessage(None, (ToolCall('w1', 'write_file', {'path': 'a.py', 'content': 'a' * 3000}),)),
                ToolMessage('w1', 'ok'),
                AssistantMessage(None, (ToolCall('w2', 'write_file', {'path': 'a.py', 'content': 'b' * 3000}),)),
                ToolMessage('w2', 'ok'))
    manager = ContextManager(window_tokens=3000, output_tokens=300, safety_tokens=100)
    plain, compacted = Recorder(ModelTurn('ok')), Recorder(ModelTurn('ok'))
    await manager.generate(plain, messages, ())
    await manager.generate(compacted, messages, (), compact=True)
    assert 'a later write_file replaced this file' not in plain.requests[0]
    assert 'a later write_file replaced this file' in compacted.requests[0]
