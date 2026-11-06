"""Every benchmark task is well formed and its hidden grader accepts a reference answer (D-035).

No model calls. Python tasks ship a hidden reference solution that must pass every
check; for fix tasks the public, buggy file must fail at least one check, so a task
cannot be solved by handing the input back unchanged.
"""

import json
import re

import pytest

from bench.graders import grade_answer_json, grade_python, static_guard, summarize
from bench.run_bench import load_tasks

TASKS = load_tasks(['all'], split='all')
SPLITS = ('tune', 'holdout')
CATEGORIES = ('qa', 'implement', 'fix', 'frontend')
HIDDEN = ('checker', 'expected', 'reference')
PYTHON = [t for t in TASKS if t['grader'] == 'python_checks']
FIX = [t for t in PYTHON if t['deliverable'] in t['files']]
QA = [t for t in TASKS if t['grader'] == 'answer_json']


def _id(task):
    return task['id']


@pytest.mark.parametrize('task', TASKS, ids=_id)
def test_task_shape(task):
    assert task['id'] == task['dir'].name
    assert task['split'] in SPLITS
    assert task['category'] in CATEGORIES
    assert task['grader'] in ('answer_json', 'python_checks', 'html_todo')
    assert task.get('expected_route', 'single') in ('single', 'team')
    assert task['files'] and len(set(task['files'])) == len(task['files'])
    for name in task['files']:
        assert '/' not in name and '\\' not in name and (task['dir'] / name).is_file()
    hidden = [task[key] for key in HIDDEN if key in task]
    assert not set(hidden) & set(task['files']), 'hidden files must never be copied to the agent'
    present = {p.name for p in task['dir'].iterdir()}
    assert present == {'task.json', *task['files'], *hidden}, 'stray files in the task directory'


@pytest.mark.parametrize('task', QA, ids=_id)
def test_answer_task_names_every_key(task):
    assert task['category'] == 'qa' and task.get('delivery') == 'answer'
    expected = json.loads((task['dir'] / task['expected']).read_text(encoding='utf-8'))
    assert isinstance(expected, dict) and len(expected) >= 3
    for key in expected:
        assert re.search(r'(?<![A-Za-z0-9_])' + re.escape(key) + r'(?![A-Za-z0-9_])', task['goal']), key
    assert summarize(grade_answer_json(json.dumps(expected), task['dir'] / task['expected']))[2]


@pytest.mark.parametrize('task', PYTHON, ids=_id)
def test_reference_passes_every_check(task):
    assert task['category'] in ('implement', 'fix')
    assert 'deliverable' in task and task['deliverable'] in task['write_goal']
    reference = (task['dir'] / task['reference']).read_text(encoding='utf-8')
    assert static_guard(reference) is None
    checks = grade_python(reference, task['dir'] / task['checker'])
    passed, total, ok = summarize(checks)
    assert ok, sorted(k for k, v in checks.items() if v is not True)
    assert total >= 8


@pytest.mark.parametrize('task', FIX, ids=_id)
def test_buggy_input_fails(task):
    assert task['category'] == 'fix'
    original = (task['dir'] / task['deliverable']).read_text(encoding='utf-8')
    assert not summarize(grade_python(original, task['dir'] / task['checker']))[2]


def test_both_splits_cover_each_kind():
    for split in SPLITS:
        kinds = {t['category'] for t in TASKS if t['split'] == split}
        assert {'qa', 'implement', 'fix'} <= kinds, split
    assert len([t for t in TASKS if t['split'] == 'holdout']) >= 10


def test_wilson_interval_matches_known_values():
    from bench.run_bench import wilson
    assert wilson(0, 0) == [0.0, 1.0]
    assert wilson(5, 10) == [0.237, 0.763]
    low, high = wilson(10, 10)
    assert high == 1.0 and 0.72 < low < 0.73
    assert wilson(0, 10)[0] == 0.0


def test_split_summary_groups_by_split_and_mode():
    from bench.run_bench import split_summary, wilson
    rows = [{'task': 'a', 'split': 'tune', 'mode': 'single', 'passed': True},
            {'task': 'a', 'split': 'tune', 'mode': 'single', 'passed': False},
            {'task': 'b', 'split': 'tune', 'mode': 'single', 'passed': True},
            {'task': 'c', 'split': 'holdout', 'mode': 'single', 'passed': False}]
    table = split_summary(rows)
    assert table['tune/single'] == {'passed': 2, 'runs': 3, 'tasks': 2, 'pass_rate': 0.667,
                                    'ci95': wilson(2, 3)}
    assert table['holdout/single']['pass_rate'] == 0.0


def test_held_out_tasks_run_only_when_asked(capsys):
    from bench import run_bench
    holdout = {t['id'] for t in TASKS if t['split'] == 'holdout'}
    assert not holdout & {t['id'] for t in run_bench.load_tasks(['all'])}
    assert {t['id'] for t in run_bench.load_tasks(['all'], 'holdout')} == holdout
    assert run_bench.main(['--split', 'holdout', '--modes', 'single']) == 2
    out = capsys.readouterr().out
    assert "splits=['holdout']" in out
    with pytest.raises(SystemExit):
        run_bench.load_tasks(['all'], 'test')
