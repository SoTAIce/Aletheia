"""D-092 (docs/loop_stall_plan.md): the loop notices it is going round in circles. Stale rounds only
re-read or search files the model already has; three in a row bring one reminder, three more a
wrap-up. A whole read that returns the recorded text gets a pointer to the earlier result, and the
model view keeps that earlier result whole."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import TODO, AgentLoopRunner, LoopOptions
from app.agent.hooks import HookSet, using_hooks
from app.agent.loop_stall import STALL_REMINDER, STALL_WRAP_UP, UNCHANGED_FIELD, pinned_outputs, unchanged_note
from app.agent.model_view import compact_model_view
from app.agent.run_limits import RunLimits, stall_text
from app.agent.run_meter import RunMeter, metering
from app.agent.step_models import (AssistantMessage, ModelTurn, StepOutcome, SystemMessage, ToolCall,
                                   ToolDefinition, ToolMessage, ToolOutput, ToolsShownOnly)
from app.agent.trace_view import build_trace, render_text
from app.tools.tool_registry import ToolRegistry
from bench.run_bench import usage_fields
from loop_fakes import started_context

REMINDER = STALL_REMINDER.format(rounds=3)


class Scripted:
    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    async def generate(self, messages, tools):
        self.requests.append((list(messages), tools))
        return self.turns.pop(0)


def turn(*calls):
    return ModelTurn(tool_calls=tuple(ToolCall(call_id, name, args) for call_id, name, args in calls))


def read(call_id, path='a.py', **args):
    return turn((call_id, 'read_file', {'path': path, **args}))


def search(call_id, pattern, path='a.py'):
    return turn((call_id, 'search_files', {'pattern': pattern, 'path': path}))


def workspace(files):
    """read_file, search_files and write_file over a dict the test can change."""
    def read_file(args):
        return ToolOutput(True, files[args['path']])

    def search_files(args):
        return ToolOutput(True, f"{args['pattern']}: {files[args['path']].count(args['pattern'])} matches")

    def write_file(args):
        files[args['path']] = args['content']
        return ToolOutput(True, 'written')

    registry = ToolRegistry()
    for name, invoke in (('read_file', read_file), ('search_files', search_files), ('write_file', write_file)):
        registry.register(SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}),
                                          invoke=AsyncMock(side_effect=invoke)))
    return registry


async def run(model, files=None, options=None):
    files = files if files is not None else {'a.py': 'print(1)\n' * 20, 'b.py': 'b = 2\n'}
    runner = AgentLoopRunner(model, workspace(files), options or LoopOptions())
    meter = RunMeter()
    with metering(meter):
        output = await runner.run(started_context(goal='explain a.py'))
    return output, meter


def replies(model, index):
    """The tool replies the model saw in request `index`."""
    return [json.loads(m.content) for m in model.requests[index][0] if isinstance(m, ToolMessage)]


def host_notes(model, index=-1):
    return [m.content for m in model.requests[index][0] if isinstance(m, SystemMessage) and m.content.startswith('HOST')]


async def test_an_unchanged_whole_read_points_to_the_earlier_result():
    model = Scripted(read('r1'), read('r2'), ModelTurn('done'))
    output, meter = await run(model)
    first, second = replies(model, 2)
    assert first['content'] == 'print(1)\n' * 20 and UNCHANGED_FIELD not in first
    assert second['content'] == unchanged_note('r1') and second[UNCHANGED_FIELD] == 'r1'
    # The receipt keeps the real result; only the model's view changed.
    assert output.tool_results[1].content == 'print(1)\n' * 20
    assert meter.snapshot()['stall'] == {'unchanged_read': 1}


async def test_a_changed_file_and_a_ranged_read_return_their_text():
    files = {'a.py': 'old\n'}
    model = Scripted(read('r1'), turn(('w', 'write_file', {'path': 'a.py', 'content': 'new\n'})),
                     read('r2'), read('r3', start_line=1, max_lines=1), ModelTurn('done'))
    await run(model, files)
    contents = [r['content'] for r in replies(model, 4)]
    assert contents == ['old\n', 'written', 'new\n', 'new\n']


async def test_three_stale_rounds_bring_one_reminder():
    model = Scripted(read('r1'), search('s1', 'print'), search('s2', 'x'), read('r2'), ModelTurn('done'))
    output, meter = await run(model)
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'done'
    assert host_notes(model, 3) == [] and host_notes(model, 4) == [REMINDER]
    assert meter.snapshot()['stall'] == {'unchanged_read': 1, 'reminder': 1}


async def test_a_new_file_breaks_the_streak_and_a_todo_round_does_not():
    todo = turn(('t', TODO, {'items': [{'content': 'look', 'status': 'in_progress'}]}))
    model = Scripted(read('r1'), search('s1', 'a'), read('b', 'b.py'), search('s2', 'b'), todo,
                     search('s3', 'c'), ModelTurn('done'))
    await run(model)
    assert host_notes(model) == []     # stale, fresh, stale, (todo), stale: never three in a row
    model = Scripted(read('r1'), search('s1', 'a'), todo, search('s2', 'b'), search('s3', 'c'), ModelTurn('done'))
    await run(model)
    assert host_notes(model) == [REMINDER]


async def test_three_more_stale_rounds_after_the_reminder_wrap_up():
    stale = [search(f's{i}', f'p{i}') for i in range(6)]
    model = Scripted(read('r1'), *stale, ModelTurn('what I have'))
    output, meter = await run(model)
    assert output.outcome is StepOutcome.SUCCEEDED and output.content == 'what I have'
    messages, tools = model.requests[-1]
    assert isinstance(tools, ToolsShownOnly)
    assert host_notes(model) == [REMINDER, STALL_WRAP_UP]
    assert meter.snapshot()['stall'] == {'reminder': 1, 'wrap_up': 1}
    assert len(model.requests) == 8


async def test_without_wrap_up_the_loop_runs_to_its_round_limit():
    stale = [search(f's{i}', f'p{i}') for i in range(6)]
    model = Scripted(read('r1'), *stale, ModelTurn('done'))
    _, meter = await run(model, options=LoopOptions(stall_wrap_up=False))
    assert not isinstance(model.requests[-1][1], ToolsShownOnly)
    assert host_notes(model) == [REMINDER] and meter.snapshot()['stall'] == {'reminder': 1}


async def test_stall_rounds_zero_turns_everything_off():
    model = Scripted(read('r1'), read('r2'), read('r3'), read('r4'), ModelTurn('done'))
    _, meter = await run(model, options=LoopOptions(stall_rounds=0))
    assert [r['content'] for r in replies(model, 4)] == ['print(1)\n' * 20] * 4
    assert host_notes(model) == [] and 'stall' not in meter.snapshot()


async def test_a_write_forgets_what_was_read():
    model = Scripted(read('r1'), search('s1', 'a'), search('s2', 'b'),
                     turn(('w', 'write_file', {'path': 'b.py', 'content': 'c\n'})),
                     search('s3', 'c'), search('s4', 'd'), ModelTurn('done'))
    await run(model)
    assert host_notes(model) == []


async def test_earlier_messages_stay_byte_identical_for_the_prompt_cache():
    stale = [read(f'r{i}') for i in range(7)]
    model = Scripted(*stale, ModelTurn('done'))
    await run(model)
    for before, after in zip(model.requests, model.requests[1:]):
        assert after[0][:len(before[0])] == before[0]


def test_the_model_view_keeps_the_pointed_to_output_whole():
    long = 'x' * 5000
    payload = lambda content, **extra: json.dumps({'success': True, 'content': content, 'error': None,
                                                   'source_refs': [], **extra})
    messages = [SystemMessage('s')]
    messages += [AssistantMessage(None, (ToolCall('r1', 'read_file', {'path': 'a.py'}),)),
                 ToolMessage('r1', payload(long)),
                 AssistantMessage(None, (ToolCall('r0', 'read_file', {'path': 'z.py'}),)),
                 ToolMessage('r0', payload(long))]
    for i in range(8):
        messages += [AssistantMessage(None, (ToolCall(f'n{i}', 'read_file', {'path': 'a.py'}),)),
                     ToolMessage(f'n{i}', payload(unchanged_note('r1'), **{UNCHANGED_FIELD: 'r1'}))]
    manager = SimpleNamespace(count_request=lambda m, t: 10_000, window_tokens=1000)
    view = {m.call_id: m for m in compact_model_view(tuple(messages), (), manager) if isinstance(m, ToolMessage)}
    assert json.loads(view['r1'].content)['content'] == long
    assert 'shortened' in json.loads(view['r0'].content)['content']
    assert pinned_outputs(messages) == {'r1'}


async def test_observers_see_only_a_kind_and_a_round_and_the_trace_shows_them():
    events = []
    model = Scripted(read('r1'), read('r2'), search('s1', 'p'), search('s2', 'q'), ModelTurn('done'))
    files = {'a.py': 'secret text\n'}
    with using_hooks(HookSet((SimpleNamespace(observe=events.append),))):
        await run(model, files)
    stalls = [e for e in events if e['type'] == 'loop_stall']
    assert stalls == [{'type': 'loop_stall', 'kind': 'unchanged_read', 'round': 2, 'parent_id': None},
                      {'type': 'loop_stall', 'kind': 'reminder', 'round': 4, 'parent_id': None}]
    assert 'secret' not in json.dumps(events)
    header = {'type': 'audit', 'version': 2, 'trace': True, 'events': 0, 'dropped': 0}
    records = [header, {'t': 0.0, 'type': 'task_start', 'task_id': 'abcdef12', 'span_id': 'T', 'parent_id': None},
               {'t': 0.1, 'type': 'before_model', 'purpose': 'step', 'span_id': 'M', 'parent_id': 'T'},
               {'t': 0.2, 'type': 'loop_stall', 'kind': 'reminder', 'round': 3, 'parent_id': 'T'},
               {'t': 0.3, 'type': 'loop_stall', 'kind': 'mystery', 'round': 4, 'parent_id': 'T'}]
    text = render_text(build_trace(records))
    assert '原地打转，提醒一次（第 3 轮）' in text and 'mystery' not in text


def test_bench_rows_and_the_limits_table_carry_the_rule():
    fields = usage_fields({'usage': {'stall': {'reminder': 1, 'wrap_up': 1, 'bad': 'x'}}})
    assert fields['stall'] == {'reminder': 1, 'wrap_up': 1}
    assert 'stall' not in usage_fields({'usage': {'model_calls': 1}})
    assert stall_text(LoopOptions()) == '3 轮后提醒一次，再 3 轮后提前收尾'
    assert stall_text(LoopOptions(stall_wrap_up=False)) == '3 轮后提醒一次，不提前收尾'
    assert stall_text(LoopOptions(stall_rounds=0)) == '关闭'
    rows = RunLimits(LoopOptions(), None, SimpleNamespace(member_timeout_seconds=1, max_delegations=1,
                                                          member_tool_calls=1), 1, 1, 1, 1).rows()
    assert ('任务', '原地打转：连续几轮后提醒，再几轮后收尾', '3 轮后提醒一次，再 3 轮后提前收尾') in rows


def test_members_get_the_rule_by_default_and_bad_options_are_refused():
    assert LoopOptions(8, 16, 120.0).stall_rounds == 3 and LoopOptions(8, 16, 120.0).stall_wrap_up is True
    for bad in ({'stall_rounds': -1}, {'stall_rounds': True}, {'stall_wrap_up': 1}):
        with pytest.raises(ValueError):
            LoopOptions(**bad)


# Plan section 11: searches and listings over a directory.

def tree(files, truncated=False, skipped=()):
    """search_files and list_directory shaped like the filesystem server's results, over a dict."""
    import fnmatch
    import posixpath

    def search_files(args):
        base, pattern = args.get('path', '.'), args.get('glob')
        names = [n for n in sorted(files) if (base == '.' or n == base or n.startswith(base + '/'))
                 and n not in skipped
                 and (pattern is None or fnmatch.fnmatch(posixpath.basename(n), pattern) or fnmatch.fnmatch(n, pattern))]
        matches = [{'path': n, 'line': i, 'text': line} for n in names
                   for i, line in enumerate(files[n].splitlines(), 1) if args['pattern'] in line]
        return ToolOutput(True, json.dumps({'matches': matches, 'files_searched': len(names),
                                            'files_skipped': 0, 'truncated': truncated}))

    def list_directory(args):
        offset, limit = args.get('offset', 0), args.get('limit', 50)
        entries = [{'name': n, 'type': 'file'} for n in sorted(files)]
        return ToolOutput(True, json.dumps({'entries': entries[offset:offset + limit], 'offset': offset,
                                            'limit': limit, 'total': len(entries),
                                            'has_more': offset + limit < len(entries)}))

    registry = workspace(files)
    for name, invoke in (('search_files', search_files), ('list_directory', list_directory)):
        registry._tools.pop(name, None)
        registry.register(SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}),
                                          invoke=AsyncMock(side_effect=invoke)))
    return registry


async def run_tree(model, files, **kwargs):
    runner = AgentLoopRunner(model, tree(files, **kwargs), LoopOptions())
    with metering(RunMeter()):
        return await runner.run(started_context(goal='explain the spec'))


def find(call_id, pattern, path='.', **args):
    return turn((call_id, 'search_files', {'pattern': pattern, 'path': path, **args}))


def listing(call_id, **args):
    return turn((call_id, 'list_directory', {'path': '.', **args}))


ONE = {'spec.md': 'title\nuse localStorage\n'}


async def test_searching_a_directory_whose_files_are_all_read_is_stale_hit_or_miss():
    # The todo_html run: one file read whole, then searches over the directory with new words.
    model = Scripted(read('r1', 'spec.md'), find('s1', 'localStorage'), find('s2', 'nothing'), find('s3', 'title'),
                     find('s4', 'x'), find('s5', 'y'), find('s6', 'z'), ModelTurn('done'))
    await run_tree(model, dict(ONE))
    assert host_notes(model) == [REMINDER, STALL_WRAP_UP]
    assert isinstance(model.requests[-1][1], ToolsShownOnly)


@pytest.mark.parametrize('case', ['unread file', 'truncated', 'hidden read', 'not json'])
async def test_a_directory_search_that_could_show_something_new_is_not_stale(case):
    files, kwargs, first = {**ONE, 'other.md': 'more\n'}, {}, read('r1', 'spec.md')
    if case == 'truncated':
        files, kwargs = dict(ONE), {'truncated': True}
    if case == 'hidden read':
        # The search skips hidden folders, so its count says nothing about the file read there.
        files, kwargs, first = {'.cfg/a.md': 'a\n', 'b.md': 'b\n'}, {'skipped': ('.cfg/a.md',)}, read('r1', '.cfg/a.md')
    model = Scripted(first, *[find(f's{i}', f'w{i}') for i in range(3)], ModelTurn('done'))
    registry = tree(files, **kwargs)
    if case == 'not json':
        registry._tools['search_files'].invoke = AsyncMock(return_value=ToolOutput(True, 'no matches'))
    runner = AgentLoopRunner(model, registry, LoopOptions())
    with metering(RunMeter()):
        await runner.run(started_context(goal='explain the spec'))
    assert host_notes(model) == []


async def test_a_hit_in_an_unread_file_and_the_glob_count_decide_staleness():
    files = {'a.json': '{"k": 1}\n', 'b.json': '{"k": 2}\n', 'loader.py': 'k = 1\n'}
    reads = [read('r1', 'a.json'), read('r2', 'b.json')]
    # Only the json files are read: a json-only search is stale, a search over everything is not.
    model = Scripted(*reads, *[find(f's{i}', 'k' * (i + 1), glob='*.json') for i in range(3)], ModelTurn('done'))
    await run_tree(model, dict(files))
    assert host_notes(model) == [REMINDER]
    model = Scripted(*reads, *[find(f's{i}', 'k' * (i + 1)) for i in range(3)], ModelTurn('done'))
    await run_tree(model, dict(files))
    assert host_notes(model) == []


async def test_listing_a_directory_again_is_stale_once_it_was_listed_in_full():
    model = Scripted(listing('l1'), read('r1', 'spec.md'), listing('l2', limit=200), listing('l3', offset=0, limit=100),
                     find('s1', 'q'), ModelTurn('done'))
    await run_tree(model, dict(ONE))
    assert host_notes(model) == [REMINDER]
    # A first listing that did not show everything proves nothing about the next page.
    model = Scripted(read('r1', 'spec.md'), listing('l1', limit=1), listing('l2', offset=1, limit=1),
                     listing('l3', offset=1, limit=2), ModelTurn('done'))
    await run_tree(model, {**ONE, 'z.md': 'z\n'})
    assert host_notes(model) == []


async def test_a_write_forgets_the_listing_too():
    model = Scripted(listing('l1'), read('r1', 'spec.md'),
                     turn(('w', 'write_file', {'path': 'new.md', 'content': 'n\n'})),
                     listing('l2', limit=200), listing('l3', limit=100), find('s1', 'q'), ModelTurn('done'))
    await run_tree(model, dict(ONE))
    assert host_notes(model) == []


def test_the_skipped_directories_match_the_filesystem_search():
    from app.agent.loop_stall import SEARCH_SKIP_DIRS
    from app.tools.filesystem.file_access import FileAccess
    assert SEARCH_SKIP_DIRS == FileAccess._SEARCH_SKIP_DIRS
