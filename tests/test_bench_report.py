"""D-086: the bench runs with a per-run archive, puts each failure down to someone, and reports
k-run and agent-only statistics with a host-built headline. No model is called: launch_agent is a fake."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import run_bench
from bench.run_bench import attribute, headline, provider_state_counts, split_summary, summary, wilson

TASK = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
GOOD = {'max_retries': 3, 'fee_function': 'late_fee', 'fee_200_15': 10, 'fee_200_60': 20.0, 'retry_delays': [1, 2, 4]}


def _launch(record=None, saved=0):
    """A fake agent process: writes the --result-json record and `saved` archived tool results."""
    def launch(cmd, env, timeout):
        archive = env['CONVERSATION_STORAGE_DIR']
        if archive and saved:
            artifacts = Path(archive) / 'session-abc' / 'artifacts'
            artifacts.mkdir(parents=True)
            for i in range(saved):
                (artifacts / f'a{i}.json').write_text('{}', encoding='utf-8')
        if record is not None:
            Path(cmd[cmd.index('--result-json') + 1]).write_text(json.dumps(record), encoding='utf-8')
        return SimpleNamespace(stdout='', stderr='')
    return launch


def _record(**fields):
    return {'status': 'completed', 'content': json.dumps(GOOD), 'execution_count': 1, 'replan_count': 0, **fields}


# ---- the per-run archive ----

def test_archived_tool_results_and_artifact_reads_are_counted(monkeypatch):
    tools = [{'tool': 'read_context_artifact', 'success': True}, {'tool': 'read_file', 'success': True},
             {'tool': 'read_context_artifact', 'success': True}]
    monkeypatch.setattr(run_bench, 'launch_agent', _launch(_record(steps=[{'tools': tools}]), saved=2))
    row = run_bench.run_one(TASK, 'single', 1, 5)
    assert row['passed'] is True and 'attribution' not in row
    assert row['archive'] is True and row['archived_tool_results'] == 2 and row['artifact_reads'] == 2


def test_without_an_archive_nothing_is_counted(monkeypatch):
    monkeypatch.setattr(run_bench, 'launch_agent', _launch(_record(), saved=2))
    row = run_bench.run_one(TASK, 'single', 1, 5, archive=False)
    assert row['archive'] is False and row['archived_tool_results'] == 0


def test_the_plan_line_names_the_archive(capsys):
    assert run_bench.main(['--tasks', 'billing_qa', '--modes', 'single']) == 2
    assert 'archive=on' in capsys.readouterr().out
    assert run_bench.main(['--tasks', 'billing_qa', '--modes', 'single', '--no-archive']) == 2
    assert 'archive=off' in capsys.readouterr().out


# ---- attribution ----

PROVIDER = 'StepModelError: Model generation failed: ModelRequestError -> APIStatusError'


@pytest.mark.parametrize('row, who', [
    ({'passed': True}, None),
    ({'passed': False, 'failure_kind': 'model', 'failure': PROVIDER}, 'provider'),
    ({'passed': False, 'failure_kind': 'model', 'failure': 'StepModelError: Model generation failed: ModelTimeoutError'},
     'provider'),
    ({'passed': False, 'failure_kind': 'model',
      'failure': 'StepModelError: Model generation failed: ModelOutputLimitError'}, 'agent'),
    ({'passed': False, 'failure_kind': 'model',
      'failure': 'StepModelError: Model generation failed: ModelToolArgumentsError'}, 'agent'),
    ({'passed': False, 'failure_kind': 'model', 'failure': 'unrecognized'}, 'unknown'),
    ({'passed': False, 'status': 'timeout', 'failure': 'timeout'}, 'unknown'),
    ({'passed': False, 'grader_error': 'RuntimeError', 'failure_kind': 'model'}, 'grader'),
    ({'passed': False, 'failure_kind': 'context', 'failure': 'StepModelError: x'}, 'agent'),
    ({'passed': False, 'failure_kind': 'tool', 'failure': 'StepToolError: Tool read_file raised OSError'}, 'agent'),
    ({'passed': False, 'status': 'completed', 'failed_checks': ['fee_200_60']}, 'agent'),
    ({'passed': False, 'failure': 'deliverable_missing'}, 'agent'),
])
def test_attribution(row, who):
    assert attribute(row) == who


def test_a_failed_run_carries_its_attribution(monkeypatch):
    record = _record(status='failed', content=PROVIDER, failure_kind='model')
    monkeypatch.setattr(run_bench, 'launch_agent', _launch(record))
    row = run_bench.run_one(TASK, 'single', 1, 5)
    assert row['passed'] is False and row['failure'] == PROVIDER and row['attribution'] == 'provider'


def test_a_grader_that_raises_fails_only_its_row(monkeypatch):
    monkeypatch.setattr(run_bench, 'launch_agent', _launch(_record()))

    def broken(task, content):
        raise RuntimeError('C:/private/grader/state')
    monkeypatch.setattr(run_bench, 'grade', broken)
    row = run_bench.run_one(TASK, 'single', 1, 5)
    assert row['passed'] is False and row['grader_error'] == 'RuntimeError' and row['attribution'] == 'grader'
    assert 'private' not in json.dumps(row)


# ---- statistics ----

def _row(task, passed, attribution=None, state='heavy', mode='single', split='tune', **extra):
    row = {'task': task, 'split': split, 'mode': mode, 'passed': passed, 'provider_state': state, **extra}
    if attribution:
        row['attribution'] = attribution
    return row


def test_k_runs_and_the_agent_only_rate():
    rows = [_row('a', True), _row('a', True), _row('a', True),
            _row('b', True), _row('b', False, 'agent'), _row('b', False, 'provider', state='padded')]
    cell = split_summary(rows)['tune/single']
    assert cell['runs_per_task'] == [3, 3] and cell['tasks_all_passed'] == 1 and cell['all_passed_rate'] == 0.5
    assert cell['excluded'] == {'provider': 1, 'grader': 0}
    assert (cell['agent_runs'], cell['agent_passed'], cell['agent_pass_rate']) == (5, 4, 0.8)
    assert cell['agent_ci95'] == wilson(4, 5) and cell['ci95'] == wilson(4, 6)
    table = summary(rows)
    assert table['a/single']['all_passed'] is True and table['b/single']['all_passed'] is False
    assert table['b/single']['provider_failures'] == 1 and table['b/single']['grader_failures'] == 0


def test_the_headline_states_the_numbers():
    rows = [_row('a', True), _row('a', True), _row('a', True),
            _row('b', True), _row('b', False, 'agent'), _row('b', False, 'provider', state='padded',
                                                             model_mismatch=True)]
    [line] = headline(rows, split_summary(rows))
    assert line == ('tune/single：6 次运行通过 4 次（67%，95% 区间 30%～90%）；每题 3 次，全过 1/2 题；'
                    '不计服务商失败 1 次、评分失败 0 次后为 4/5（80%，区间 38%～96%）；'
                    '服务商状态 padded 1、heavy 5；回答模型和请求不同 1 次。')


def test_uneven_runs_are_flagged_and_an_empty_agent_denominator_is_said():
    rows = [_row('a', False, 'provider'), _row('b', False, 'grader'), _row('b', False, 'provider')]
    cell = split_summary(rows)['tune/single']
    assert cell['runs_per_task'] == [1, 2] and cell['agent_runs'] == 0 and cell['agent_pass_rate'] is None
    [line] = headline(rows, {'tune/single': cell})
    assert '每题 1～2 次' in line and '仅供参考' in line and '没有可计的运行' in line


def test_the_headline_takes_no_text_from_the_rows():
    rows = [_row('<script>', False, 'agent', state='<b>evil</b>', split='Tune Set!',
                 failure='StepBusinessError: SECRET model text')]
    lines = headline(rows, split_summary(rows))
    text = '\n'.join(lines)
    assert lines[0].startswith('?：') and 'unknown 1' in text
    for leaked in ('script', 'evil', 'Tune', 'SECRET'):
        assert leaked not in text


def test_provider_state_counts():
    rows = [_row('a', True), _row('a', True, state='plain'), _row('a', True, state='weird'),
            {'task': 'a', 'mode': 'single', 'passed': True}]
    assert provider_state_counts(rows) == {'dropped': 0, 'plain': 1, 'padded': 0, 'heavy': 1, 'unknown': 2}
