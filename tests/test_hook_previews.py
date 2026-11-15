"""Bounded approval previews must never enter observer logs."""
import json
from types import SimpleNamespace

import pytest

from app.agent import hooks
from app.agent.builtin_hooks import TraceFileHook
from app.agent.step_models import ToolDefinition, ToolOutput


def test_write_preview_is_bounded_and_counts_omitted_characters():
    for size in (0, 19999, 20000, 20001):
        preview = hooks.preview_arguments('write_file', {
            'path': 'a.py', 'overwrite': True, 'content': '字' * size, 'extra': 'private'})
        assert preview == {'path': 'a.py', 'overwrite': True, 'content': {
            'text': '字' * min(size, 20000), 'chars': size, 'omitted_chars': max(0, size - 20000)}}


def test_python_preview_limits_items_and_counts_all_dropped_characters():
    preview = hooks.preview_arguments('run_python', {'path': 'a.py', 'args': ['字' * 203] * 18})
    assert preview == {'path': 'a.py', 'args': {'items': ['字' * 200] * 16,
        'total_items': 18, 'omitted_items': 2, 'omitted_chars': 16 * 3 + 2 * 203}}
    assert hooks.preview_arguments('run_python', {'args': []})['args'] == {
        'items': [], 'total_items': 0, 'omitted_items': 0, 'omitted_chars': 0}


def test_tests_preview_contains_only_path():
    assert hooks.preview_arguments('run_tests', {'path': 'tests', 'extra': 'private'}) == {'path': 'tests'}


def test_other_previews_bound_strings_and_summarize_non_strings():
    args = {'query': 'q' * 2001, 'paths': ['secret'], 'nested': {'secret': 'body'},
            'limit': 2, 'value': None}
    assert hooks.preview_arguments('other', args) == {
        'query': {'text': 'q' * 2000, 'chars': 2001, 'omitted_chars': 1},
        'paths': {'items': 1}, 'nested': {'items': 1}, 'limit': 2, 'value': {'type': 'NoneType'}}


@pytest.mark.parametrize('tool', ['write_file', 'run_python', 'run_tests', 'other', None, []])
@pytest.mark.parametrize('args', [None, [], 42, 'oops', {1: object()},
    {'path': object(), 'overwrite': [], 'content': 42, 'args': 'wrong'},
    {'content': None, 'args': [None, {}, ['private'], object()]}])
def test_odd_preview_inputs_are_safe_and_json_serializable(tool, args):
    assert isinstance(hooks.preview_arguments(tool, args), dict)
    json.dumps(hooks.preview_arguments(tool, args))


async def test_approved_write_preview_is_gate_only_and_trace_has_no_content(tmp_path):
    from app.api.approvals import ApprovalGate
    from app.api.runs import Run
    import asyncio

    content = '<script>private approval content</script>'
    events = []
    observer = SimpleNamespace(name='record', observe=events.append)
    trace = TraceFileHook(tmp_path / 'trace')
    run = Run('r', 's', '', None, None)
    gate = ApprovalGate(run, frozenset({'write_file'}), 5)
    received = []
    async def capture(event):
        received.append(event)
        return await gate.check(event)
    capturing_gate = SimpleNamespace(name='capture', events=gate.events, check=capture)
    class Write:
        definition = ToolDefinition('write_file', 'Write', {'type': 'object'})
        async def invoke(self, arguments):
            (tmp_path / arguments['path']).write_text(arguments['content'], encoding='utf-8')
            return ToolOutput(True, 'written')
    with hooks.using_hooks(hooks.HookSet([observer, trace], [capturing_gate])) as installed:
        task = asyncio.create_task(hooks.HookedTool(Write()).invoke({'path': 'out.txt', 'content': content}))
        async with asyncio.timeout(2):
            while not run.approvals:
                await asyncio.sleep(0)
        next(iter(run.approvals.values())).set_result('allow')
        assert (await task).success
        installed.emit({'type': 'task_end', 'task_id': 't'})
    assert (tmp_path / 'out.txt').read_text(encoding='utf-8') == content
    assert received[0]['args_preview']['content']['text'] == content
    assert run.events[0]['args_preview'] == received[0]['args_preview']
    assert all('args_preview' not in event for event in events)
    assert content not in json.dumps(events)
    logged = trace.path.read_text(encoding='utf-8')
    assert 'args_preview' not in logged and content not in logged
    assert json.loads(logged.splitlines()[1])['args_summary']['content'] == {'chars': len(content)}


@pytest.mark.parametrize('tool', ['write_file', 'run_python', 'run_tests'])
def test_known_tool_paths_are_bounded(tool):
    preview = hooks.preview_arguments(tool, {'path': 'p' * 2001})
    assert preview['path'] == {'text': 'p' * 2000, 'chars': 2001, 'omitted_chars': 1}


def test_short_python_arguments_and_unknown_values():
    assert hooks.preview_arguments('run_python', {'args': ['a', '', '字' * 200, None]})['args'] == {
        'items': ['a', '', '字' * 200, {'type': 'NoneType'}], 'total_items': 4,
        'omitted_items': 0, 'omitted_chars': 0}


def test_preview_does_not_call_odd_container_or_string_accessors():
    class OddDict(dict):
        def items(self):
            raise ValueError('private')
    class OddText(str):
        def __len__(self):
            raise ValueError('private')
        def __getitem__(self, key):
            raise ValueError('private')
    class OddList(list):
        def __len__(self):
            raise ValueError('private')
    assert hooks.preview_arguments('write_file', OddDict(content=OddText('abc'))) == {
        'content': {'text': 'abc', 'chars': 3, 'omitted_chars': 0}}
    assert hooks.preview_arguments('other', {'odd': OddList()}) == {'odd': {'type': 'OddList'}}
