"""Tracing (trace-001, D-073): span ids on hook events, the trace tree, reading it back."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent import trace as trace_cli
from app.agent.builtin_hooks import AUDIT_VERSION, TraceFileHook
from app.agent.hooks import (
    ALLOW, Decision, HookSet, HookedModel, HookedTool, PolicyDeniedError, current_span, root_span,
    trace_scope, using_hooks,
)
from app.agent.run_meter import RunMeter, metering, record_model_call
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput, UserMessage
from app.agent.trace_view import build_trace, render_text
from app.models.taskstate import TaskStatus
from app.services.conversation_archive import ConversationArchive
from app.services.rag_agent_service import RagAgentService


class Recorder:
    name = 'recorder'

    def __init__(self):
        self.events = []

    def observe(self, event):
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if e['type'] == kind]


class Deny:
    def __init__(self, *events):
        self.name, self.events = 'deny', frozenset(events)

    async def check(self, event):
        return Decision.deny('no')


class Tool:
    definition = ToolDefinition('read_file', 'read', {'type': 'object', 'properties': {}})

    def __init__(self, fail=False):
        self.fail = fail

    async def invoke(self, arguments):
        if self.fail:
            raise RuntimeError('broken')
        return ToolOutput(True, 'abc')


class Model:
    def __init__(self, *turns, fail=False, name='upstream-model-1'):
        self.turns, self.fail, self.name = list(turns), fail, name

    async def generate(self, messages, tools):
        record_model_call(SimpleNamespace(prompt_tokens=100, completion_tokens=5), None, self.name)
        if self.fail:
            raise RuntimeError('provider down')
        return self.turns.pop(0) if self.turns else ModelTurn('ok')


MESSAGES = (UserMessage('hi'),)


def pair(recorder, before, after):
    return recorder.of(before), recorder.of(after)


# ---- 1. span ids on events --------------------------------------------------------------

@pytest.mark.parametrize('case', ['ok', 'error', 'denied'])
async def test_a_model_call_has_one_span_id_on_both_events(case):
    recorder = Recorder()
    gates = [Deny('before_model')] if case == 'denied' else []
    model = HookedModel(Model(fail=case == 'error'))
    with using_hooks(HookSet([recorder], gates)), metering(RunMeter()):
        if case == 'ok':
            await model.generate(MESSAGES, ())
        else:
            with pytest.raises((RuntimeError, PolicyDeniedError)):
                await model.generate(MESSAGES, ())
    (before,), (after,) = pair(recorder, 'before_model', 'after_model')
    assert before['span_id'] == after['span_id'] and before['span_id'].startswith('s-')
    assert before['parent_id'] is None and after['parent_id'] is None
    if case == 'ok':
        assert after['response_model'] == 'upstream-model-1'      # a label, not provider text


@pytest.mark.parametrize('case', ['ok', 'error', 'denied'])
async def test_a_tool_call_has_one_span_id_on_both_events(case):
    recorder = Recorder()
    gates = [Deny('before_tool')] if case == 'denied' else []
    tool = HookedTool(Tool(fail=case == 'error'))
    with using_hooks(HookSet([recorder], gates)):
        with trace_scope('step', index=1) as step:
            if case == 'error':
                with pytest.raises(RuntimeError):
                    await tool.invoke({'path': 'a.txt'})
            else:
                await tool.invoke({'path': 'a.txt'})
    (before,), (after,) = pair(recorder, 'before_tool', 'after_tool')
    assert before['span_id'] == after['span_id'] and before['parent_id'] == after['parent_id'] == step


async def test_scopes_nest_and_calls_name_the_innermost_scope():
    recorder = Recorder()
    model = HookedModel(Model())
    with using_hooks(HookSet([recorder])), metering(RunMeter()):
        with root_span() as task:
            with trace_scope('step', index=1, revision=2) as step:
                with trace_scope('member', member='worker-1') as member:
                    await model.generate(MESSAGES, ())
                assert current_span() == step
    starts = recorder.of('scope_start')
    assert [(s['kind'], s['parent_id']) for s in starts] == [('step', task), ('member', step)]
    assert starts[0]['index'] == 1 and starts[0]['revision'] == 2 and starts[1]['member'] == 'worker-1'
    assert recorder.of('before_model')[0]['parent_id'] == member
    assert [e['error'] for e in recorder.of('scope_end')] == [None, None]
    assert current_span() is None


async def test_parallel_members_keep_their_own_scopes():
    recorder = Recorder()
    model = HookedModel(Model())

    async def member(name):
        with trace_scope('member', member=name) as span:
            await asyncio.sleep(0)
            await model.generate(MESSAGES, ())
            await asyncio.sleep(0)
            await model.generate(MESSAGES, ())
            return span
    with using_hooks(HookSet([recorder])), metering(RunMeter()):
        with trace_scope('step', index=1):
            first, second = await asyncio.gather(member('a'), member('b'))
    parents = [e['parent_id'] for e in recorder.of('before_model')]
    assert sorted(parents) == sorted([first, first, second, second]) and first != second


async def test_a_scope_passes_exceptions_and_cancellation_through_unchanged():
    recorder = Recorder()
    with using_hooks(HookSet([recorder])):
        with pytest.raises(KeyError):
            with trace_scope('step', index=1):
                raise KeyError('x')

        async def stuck():
            with trace_scope('plan', revision=1):
                await asyncio.sleep(10)
        task = asyncio.ensure_future(stuck())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [e['error'] for e in recorder.of('scope_end')] == ['KeyError', 'CancelledError']


async def test_without_hooks_nothing_is_emitted_and_calls_pass_straight_through():
    inner = AsyncMock()
    inner.generate.return_value = ModelTurn('x')
    with trace_scope('step', index=1) as span, root_span() as task:
        assert span is None and task is None
        assert (await HookedModel(inner).generate(MESSAGES, ())).content == 'x'
    inner.generate.assert_awaited_once_with(MESSAGES, ())


def test_labels_are_checked_and_long_member_names_cut():
    recorder = Recorder()
    with using_hooks(HookSet([recorder])):
        with pytest.raises(ValueError):
            with trace_scope('step', text='model words'):
                pass
        with pytest.raises(TypeError):
            with trace_scope('step', index='1'):
                pass
        with pytest.raises(ValueError):
            with trace_scope('thinking'):
                pass
        with trace_scope('member', member='w' * 200):
            pass
    assert len(recorder.of('scope_start')[0]['member']) == 64


def test_model_names_are_short_printable_labels_only():
    meter = RunMeter()
    for name in ('qwen-plus', ' ', 'x' * 101, 'bad\nname', None, 42):
        meter.record(SimpleNamespace(prompt_tokens=1, completion_tokens=1), None, name)
    assert meter.response_models == ['qwen-plus', None, None, None, None, None]
    assert 'response_models' not in meter.snapshot() and 'model' not in meter.snapshot()['calls'][0]


# ---- 2/3. the runtime and the team -------------------------------------------------------

def outcome(text, status='succeeded', reason=None):
    return ModelTurn(json.dumps({'status': status, 'content': text, 'reason': reason}))


TIME_CALL = ModelTurn(tool_calls=(ToolCall('c1', 'get_current_time', {}),))


def service(tmp_path, turns, steps=(('Look', 'Answer'),), runner='plan', hooks=None):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.side_effect = [json.dumps({'steps': list(s), 'reason': None}) for s in steps]
    model.generate.side_effect = list(turns)
    return RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                           memory_directory=tmp_path, single_runner=runner, hooks=hooks)


def tree_shape(span):
    return (span['kind'], [tree_shape(child) for child in span['children']])


async def test_a_two_step_plan_run_is_a_tree_of_steps():
    progress = []
    recorder = Recorder()
    svc = service(None, [TIME_CALL, outcome('a'), outcome('b')], hooks=HookSet([recorder]))
    async with svc:
        result = await svc.query_result('What time?', 's', on_event=progress.append)
    assert result.status is TaskStatus.COMPLETED
    trace = build_trace(recorder.events)
    task, = trace['spans']
    assert tree_shape(task) == ('task', [('plan', []), ('step', [('model', []), ('tool', []), ('model', [])]),
                                         ('step', [('model', [])])])
    assert trace['complete'] and trace['status'] == 'completed' and trace['totals']['tool_calls'] == 1
    assert task['children'][1]['attrs']['runner'] == 'tool_calling'
    # The progress callback still sees only its two record types, without span ids.
    assert {e['type'] for e in progress} <= {'plan', 'step', 'route'}
    assert not any('span_id' in e for e in progress)


async def test_a_loop_run_hangs_its_calls_under_its_one_step(tmp_path):
    recorder = Recorder()
    svc = service(tmp_path, [TIME_CALL, ModelTurn('final answer')], runner='loop', hooks=HookSet([recorder]))
    async with svc:
        result = await svc.query_result('What time?', 's')
        traces = await svc.task_traces('s', result.task_id)
    assert result.status is TaskStatus.COMPLETED
    run, = traces
    task, = run['spans']
    assert tree_shape(task) == ('task', [('plan', []), ('step', [('model', []), ('tool', []), ('model', [])])])
    assert task['children'][1]['attrs']['runner'] == 'agent_loop'
    text = json.dumps(traces, ensure_ascii=False)
    assert 'final answer' not in text and 'What time' not in text


async def test_team_member_calls_hang_under_the_member_and_the_lead_under_the_step():
    from test_sandbox_step1 import _context
    from test_sandbox_step3 import FakeTool, call
    from test_tool_argument_repair import Scripted
    from app.agent.team_v2 import DelegatingTeamRunner, DelegationOptions
    from app.tools.tool_registry import ToolRegistry
    model = Scripted(call('l1', 'delegate', task='read notes.txt and summarize'),
                     call('w1', 'read_file', path='notes.txt'),
                     ModelTurn(content=json.dumps({'status': 'succeeded', 'content': 'x', 'reason': None})),
                     ModelTurn(content='The notes say x = 1.'))
    registry = ToolRegistry()
    registry.register(HookedTool(FakeTool('read_file', 'x = 1')))
    recorder = Recorder()
    with using_hooks(HookSet([recorder])), metering(RunMeter()):
        with root_span(), trace_scope('step', index=1):
            await DelegatingTeamRunner(HookedModel(model), registry, DelegationOptions()).run(_context())
    trace = build_trace(recorder.events)
    task, = trace['spans']
    step, = task['children']
    kinds = [child['kind'] for child in step['children']]
    assert kinds == ['model', 'member', 'model']                       # the Lead, the worker, the Lead
    member = step['children'][1]
    assert member['label'] == 'worker-1'
    assert [child['kind'] for child in member['children']] == ['model', 'tool', 'model']


# ---- 4/5. build_trace and render_text -----------------------------------------------------

HEADER = {'type': 'audit', 'version': 2, 'trace': True, 'started_at': '2026-11-10T00:00:00+00:00',
          'events': 0, 'dropped': 0, 'mode': 'single', 'read_only': False}


def sample():
    return [HEADER,
            {'t': 0.0, 'type': 'task_start', 'task_id': '0ccc29ef-aaaa', 'span_id': 'T', 'parent_id': None},
            {'t': 0.1, 'type': 'scope_start', 'span_id': 'S', 'parent_id': 'T', 'kind': 'step', 'index': 1},
            {'t': 0.1, 'type': 'before_model', 'purpose': 'step', 'span_id': 'M1', 'parent_id': 'S'},
            {'t': 3.2, 'type': 'after_model', 'purpose': 'step', 'success': True, 'seconds': 3.1,
             'prompt_tokens': 1901, 'completion_tokens': 50, 'cached_tokens': 0, 'tool_calls': 1,
             'span_id': 'M1', 'parent_id': 'S'},
            {'t': 3.2, 'type': 'before_tool', 'tool': 'read_file', 'args_summary': {'path': 'SPEC.md'},
             'span_id': 'U1', 'parent_id': 'S'},
            {'t': 3.22, 'type': 'after_tool', 'tool': 'read_file', 'success': True, 'chars': 1204,
             'seconds': 0.02, 'span_id': 'U1', 'parent_id': 'S'},
            {'t': 3.22, 'type': 'before_model', 'purpose': 'step', 'span_id': 'M2', 'parent_id': 'S'},
            {'t': 15.6, 'type': 'after_model', 'purpose': 'step', 'success': True, 'seconds': 12.4,
             'prompt_tokens': 2785, 'completion_tokens': 449, 'span_id': 'M2', 'parent_id': 'S'},
            {'t': 15.9, 'type': 'scope_end', 'span_id': 'S', 'kind': 'step', 'seconds': 15.8, 'error': None},
            {'t': 15.9, 'type': 'step', 'step': 1, 'outcome': 'succeeded', 'runner': 'agent_loop'},
            {'t': 16.0, 'type': 'task_end', 'task_id': '0ccc29ef-aaaa', 'status': 'completed', 'span_id': 'T',
             'parent_id': None}]


def test_a_complete_file_becomes_a_tree_with_totals():
    trace = build_trace(sample())
    assert trace['version'] == 2 and trace['complete'] and trace['status'] == 'completed'
    assert trace['seconds'] == 16.0 and trace['task_id'] == '0ccc29ef-aaaa' and trace['mode'] == 'single'
    assert trace['totals'] == {'model_calls': 2, 'tool_calls': 1, 'prompt_tokens': 4686,
                               'completion_tokens': 499, 'cached_tokens': 0}
    step, = trace['spans'][0]['children']
    assert [(c['kind'], c['seconds']) for c in step['children']] == [('model', 3.1), ('tool', 0.02), ('model', 12.4)]


def test_render_text_is_a_fixed_text_tree():
    assert render_text(build_trace(sample())) == (
        '任务 0ccc29ef  completed  16.0 秒  模型 2 次  工具 1 次  输入 4,686 / 输出 499 / 缓存 0\n'
        '└─ 步骤 1  15.8 秒  执行方式 agent_loop\n'
        '   ├─ 模型 step  3.1 秒  输入 1,901  输出 50  请求工具 1\n'
        '   ├─ 工具 read_file  0.02 秒  成功  1,204 字符  path=SPEC.md\n'
        '   └─ 模型 step  12.4 秒  输入 2,785  输出 449\n')


def test_missing_ends_missing_parents_and_dropped_events_are_tolerated():
    records = sample()
    del records[8]                                                # M2 never ended
    records[5] = {**records[5], 'parent_id': 'gone'}              # its parent was dropped
    records[0] = {**HEADER, 'dropped': 3}
    trace = build_trace(records)
    assert not trace['complete'] and trace['dropped'] == 3
    task = trace['spans'][0]
    assert [c['kind'] for c in task['children']] == ['step', 'tool']
    unfinished = task['children'][0]['children'][-1]
    assert unfinished['status'] == 'unfinished' and unfinished['seconds'] is None
    assert '未结束' in render_text(trace) and '记录不完整' in render_text(trace)


@pytest.mark.parametrize('records', [[], [HEADER], [HEADER, {'type': 'mystery'}], None, ['junk', 3]])
def test_empty_or_odd_files_give_an_empty_unfinished_trace(records):
    trace = build_trace(records)
    assert trace['complete'] is False and trace['totals']['model_calls'] == 0
    render_text(trace)


def test_version_1_files_are_paired_in_order_and_listed_flat():
    records = [{'type': 'audit', 'version': 1, 'events': 6, 'dropped': 0},
               {'t': 0, 'type': 'task_start', 'task_id': 't1'},
               {'t': 0.1, 'type': 'before_model', 'purpose': 'step'},
               {'t': 1.1, 'type': 'after_model', 'purpose': 'step', 'success': True, 'seconds': 1.0,
                'prompt_tokens': 10, 'completion_tokens': 2},
               {'t': 1.1, 'type': 'before_tool', 'tool': 'read_file', 'call_id': 'c1'},
               {'t': 1.2, 'type': 'after_tool', 'tool': 'read_file', 'call_id': 'c1', 'success': True,
                'seconds': 0.1, 'chars': 3},
               {'t': 1.3, 'type': 'todo', 'items': [{'content': 'model words', 'status': 'done'}]},
               {'t': 2, 'type': 'task_end', 'task_id': 't1', 'status': 'completed'}]
    trace = build_trace(records)
    assert trace['version'] == 1 and trace['paired_by_order'] is True and trace['complete']
    task, = trace['spans']
    assert [(c['kind'], c['status']) for c in task['children']] == [('model', 'ok'), ('tool', 'ok'), ('todo', 'ok')]
    assert task['children'][2]['attrs'] == {'items': 1, 'done': 1}
    assert 'model words' not in json.dumps(trace, ensure_ascii=False)
    assert '第 1 版记录' in render_text(trace)


def test_a_route_choice_before_the_run_is_its_own_top_span():
    records = [HEADER,
               {'t': 0.0, 'type': 'scope_start', 'span_id': 'R', 'parent_id': None, 'kind': 'route'},
               {'t': 0.0, 'type': 'before_model', 'purpose': 'route', 'span_id': 'M', 'parent_id': 'R'},
               {'t': 0.5, 'type': 'after_model', 'purpose': 'route', 'success': True, 'seconds': 0.5,
                'span_id': 'M', 'parent_id': 'R'},
               {'t': 0.5, 'type': 'scope_end', 'span_id': 'R', 'kind': 'route', 'seconds': 0.5, 'error': None},
               *sample()[1:]]
    trace = build_trace(records)
    assert [s['kind'] for s in trace['spans']] == ['route', 'task']
    assert trace['spans'][0]['children'][0]['label'] == 'route'


# ---- 6. the archive ------------------------------------------------------------------------

def write_audit(archive, name, records):
    archive.save_audit(name, records)


def test_the_archive_lists_and_loads_only_its_own_audit_files(tmp_path):
    mine, other = ConversationArchive(tmp_path, 'a'), ConversationArchive(tmp_path, 'b')
    assert mine.list_audits() == []
    write_audit(mine, '20261110T000002Z-t2', sample())
    write_audit(mine, '20261110T000001Z-t1', sample())
    write_audit(other, '20261110T000003Z-t3', sample())
    assert mine.list_audits() == ['20261110T000001Z-t1', '20261110T000002Z-t2']
    records, skipped = mine.load_audit('20261110T000001Z-t1')
    assert records == sample() and skipped == 0
    with pytest.raises(KeyError):
        mine.load_audit('20261110T000003Z-t3')
    for bad in ('../x', 'a/b', '', 'x' * 121, 5):
        with pytest.raises(ValueError):
            mine.load_audit(bad)


def test_bad_lines_are_skipped_and_large_files_refused(tmp_path, monkeypatch):
    archive = ConversationArchive(tmp_path, 'a')
    path = archive.save_audit('n', sample())
    raw = str(path)
    with open(raw, 'a', encoding='utf-8') as handle:
        handle.write('{not json\n[1, 2]\n')
    records, skipped = archive.load_audit('n')
    assert len(records) == len(sample()) and skipped == 2
    monkeypatch.setattr(ConversationArchive, 'MAX_AUDIT_BYTES', 10)
    with pytest.raises(ValueError):
        archive.load_audit('n')


# ---- 7. service and web endpoint -----------------------------------------------------------

async def test_a_blocked_task_and_its_resume_are_two_runs(tmp_path):
    svc = service(tmp_path, [outcome('need it', 'blocked', '哪一句？'), outcome('Hello')], steps=(('Ask',), ('Translate',)))
    async with svc:
        blocked = await svc.query_result('翻译', 's')
        await svc.query_result('你好', 's', resume_task_id=blocked.task_id)
        runs = await svc.task_traces('s', blocked.task_id)
        assert await svc.task_traces('s', 'other-task') == []
        assert await svc.task_traces('unknown', blocked.task_id) == []
    assert [r['status'] for r in runs] == ['blocked', 'completed']
    assert all(r['task_id'] == blocked.task_id for r in runs)


async def test_without_an_archive_there_are_no_traces():
    svc = service('', [outcome('a')], steps=(('A',),))
    async with svc:
        result = await svc.query_result('q', 's')
        assert await svc.task_traces('s', result.task_id) == []


async def test_the_trace_endpoint(tmp_path):
    from test_web_api import api, AUTH
    svc = service(tmp_path, [outcome('42')], steps=(('A',),))
    async with api(svc) as web:
        response = await web.send('s1', 'q', 'r1')
        run_id = response.json()['run_id']
        await web.finished(run_id)
        task_id = (await web.client.get('/api/sessions/s1/turns')).json()['turns'][-1]['task_id']
        found = await web.client.get(f'/api/sessions/s1/tasks/{task_id}/trace')
        assert found.status_code == 200
        run, = found.json()['runs']
        assert run['status'] == 'completed' and run['spans'][0]['kind'] == 'task'
        missing = await web.client.get('/api/sessions/s1/tasks/nothing-here/trace')
        assert missing.status_code == 404 and missing.json()['code'] == 'trace_unavailable'
        bad = await web.client.get('/api/sessions/s1/tasks/bad%20id/trace')
        assert bad.status_code == 400
        anonymous = await web.client.get(f'/api/sessions/s1/tasks/{task_id}/trace',
                                         headers={'Authorization': ''})
        assert anonymous.status_code == 401


# ---- 8. the command line --------------------------------------------------------------------

def test_cli_reads_a_file_and_returns_exit_codes(tmp_path):
    path = tmp_path / 'run.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in sample()), encoding='utf-8')
    out = []
    assert trace_cli.main([str(path)], out.append) == 0
    assert out[0].startswith('任务 0ccc29ef  completed')
    out.clear()
    assert trace_cli.main([str(path), '--json'], out.append) == 0
    assert json.loads(out[0])['totals']['model_calls'] == 2
    assert trace_cli.main([str(tmp_path / 'none.jsonl')], [].append) == 1
    assert trace_cli.main([], [].append) == 2
    assert trace_cli.main([str(path), '--session', 's'], [].append) == 2
    assert trace_cli.main(['--task', 't'], [].append) == 2
    assert trace_cli.main(['--bogus'], [].append) == 2


async def test_cli_lists_a_sessions_runs_and_shows_one_task(tmp_path):
    svc = service(tmp_path, [outcome('a'), outcome('b')], steps=(('A',), ('B',)))
    async with svc:
        first = await svc.query_result('q1', 's')
        await svc.query_result('q2', 's')
    out = []
    args = ['--session', 's', '--memory-dir', str(tmp_path)]
    assert await asyncio.to_thread(trace_cli.main, args, out.append) == 0
    assert out[0] == '这个会话有 2 次运行：' and first.task_id in out[1]
    out.clear()
    assert await asyncio.to_thread(trace_cli.main, [*args, '--task', first.task_id], out.append) == 0
    assert out[0].startswith('任务 ' + first.task_id[:8])
    assert await asyncio.to_thread(trace_cli.main, [*args, '--task', 'none'], [].append) == 1
    assert await asyncio.to_thread(trace_cli.main, ['--session', 'nobody', '--memory-dir', str(tmp_path)],
                                   [].append) == 1


# ---- 9. the benchmark -------------------------------------------------------------------------

def test_the_bench_passes_the_trace_directory_to_each_run(tmp_path, monkeypatch):
    import bench.run_bench as bench
    seen = []

    def launch(cmd, env, timeout):
        seen.append(env)
        return SimpleNamespace(stdout='', stderr='', returncode=0)
    monkeypatch.setattr(bench, 'launch_agent', launch)
    monkeypatch.setenv('ALETHEIA_TRACE_DIR', 'inherited')
    task = {'id': 't1', 'dir': tmp_path, 'files': [], 'goal': 'g', 'expect': {}}
    for trace_dir in (None, tmp_path / 'traces'):
        try:
            bench.run_one(task, 'single', 2, 5, trace_dir=trace_dir)
        except Exception:
            pass                       # grading a run that did nothing is not what this test checks
    assert 'ALETHEIA_TRACE_DIR' not in seen[0]
    assert seen[1]['ALETHEIA_TRACE_DIR'] == str((tmp_path / 'traces').resolve())
    assert seen[1]['ALETHEIA_TRACE_NAME'] == 't1-single-2'


def test_the_cli_writes_trace_files_only_outside_the_agent_roots(tmp_path):
    from app.agent.__main__ import trace_hooks
    root = tmp_path / 'root'
    assert trace_hooks(str(root), None, environ={}) is None
    assert trace_hooks(str(root), None, environ={'ALETHEIA_TRACE_DIR': str(root / 'x')}) is None
    hooks = trace_hooks(str(root), None, environ={'ALETHEIA_TRACE_DIR': str(tmp_path / 'traces'),
                                                  'ALETHEIA_TRACE_NAME': 't1-single-1'})
    hook, = hooks.observers
    assert isinstance(hook, TraceFileHook)


async def test_a_trace_file_reads_back_into_the_same_tree(tmp_path):
    hook = TraceFileHook(tmp_path / 'traces', prefix='t1-single-1')
    svc = service(None, [TIME_CALL, outcome('a'), outcome('b')], hooks=HookSet([hook]))
    async with svc:
        await svc.query_result('What time?', 's')
    path, = (tmp_path / 'traces').glob('*.jsonl')
    assert path.name.startswith('t1-single-1-')
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert records[0]['version'] == AUDIT_VERSION == 2
    trace = build_trace(records)
    assert trace['complete'] and trace['totals']['tool_calls'] == 1
    assert trace_cli.main([str(path)], [].append) == 0
