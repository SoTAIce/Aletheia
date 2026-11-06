"""Deterministic tests for the read-only question task billing_qa and route accuracy rows (route-auto-001)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import run_bench
from bench.graders import grade_answer_json, summarize

TASK = next(t for t in run_bench.load_tasks(['all']) if t['id'] == 'billing_qa')
EXPECTED = TASK['dir'] / TASK['expected']
GOOD = {'max_retries': 3, 'fee_function': 'late_fee', 'fee_200_15': 10, 'fee_200_60': 20.0, 'retry_delays': [1, 2, 4]}


def test_the_task_is_read_only_and_its_answer_key_is_hidden():
    assert TASK['delivery'] == 'answer' and TASK['expected_route'] == 'single'
    assert TASK['files'] == ['billing.py'] and 'expected.json' not in TASK['files']


def test_the_answer_key_matches_the_public_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('billing', TASK['dir'] / 'billing.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actual = {'max_retries': module.MAX_RETRIES, 'fee_function': 'late_fee',
              'fee_200_15': module.late_fee(200, 15), 'fee_200_60': module.late_fee(200, 60),
              'retry_delays': module.retry_delays()}
    assert summarize(grade_answer_json(json.dumps(actual), EXPECTED)) == (5, 5, True)


@pytest.mark.parametrize('text, result', [
    (json.dumps(GOOD), (5, 5, True)),
    ('```json\n' + json.dumps(GOOD) + '\n```', (5, 5, True)),
    (json.dumps(dict(GOOD, fee_200_60=40)), (4, 5, False)),
    (json.dumps(dict(GOOD, max_retries=True)), (4, 5, False)),         # a bool is not the number 3
    (json.dumps(dict(GOOD, retry_delays=[1, 2])), (4, 5, False)),
    (json.dumps({k: v for k, v in GOOD.items() if k != 'fee_function'}), (4, 5, False)),
    ('答案如下：' + json.dumps(GOOD), (0, 0, False)),
    ('[1, 2, 4]', (0, 0, False)),
])
def test_strict_grading_needs_one_json_object(text, result):
    assert summarize(grade_answer_json(text, EXPECTED)) == result


def test_lenient_grading_reads_the_object_inside_prose():
    assert summarize(grade_answer_json('答案如下：' + json.dumps(GOOD) + ' 完毕', EXPECTED, lenient=True)) == (5, 5, True)


def _agent(answer, route=None, write=None):
    seen = {}

    def launch(cmd, env, timeout):
        seen['cmd'] = cmd
        root = Path(cmd[cmd.index('--filesystem-root') + 1])
        if write:
            (root / write).write_text('x', encoding='utf-8')
        stderr = 'Route decision: ' + json.dumps(route) + '\n' if route else ''
        return SimpleNamespace(stdout=answer + '\n\nStatus: completed; executions: 1; replans: 0\n', stderr=stderr)
    return seen, launch


def test_sandbox_auto_run_offers_tools_but_grades_the_answer(monkeypatch):
    seen, launch = _agent(json.dumps(GOOD), {'mode': 'single', 'source': 'model', 'rule': 'code_delivery_with_sandbox'})
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(TASK, 'auto', 1, 5, sandbox=True)
    cmd = seen['cmd']
    assert '--write-root' in cmd and '--sandbox' in cmd and cmd[cmd.index('--execution-mode') + 1] == 'auto'
    assert cmd[cmd.index('-m') + 2] == TASK['goal']                    # no write goal, no sandbox test hint
    assert row['delivery'] == 'answer' and row['passed'] is True and row['checks_total'] == 6
    assert row['route_correct'] is True and row['lenient_all_passed'] is True


def test_changing_the_workspace_fails_a_read_only_task(monkeypatch):
    seen, launch = _agent(json.dumps(GOOD), {'mode': 'team', 'source': 'model', 'rule': 'r'}, write='notes.txt')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(TASK, 'auto', 1, 5, sandbox=True)
    assert row['passed'] is False and row['failed_checks'] == ['workspace_unchanged']
    assert row['route_correct'] is False


def test_route_accuracy_is_summarised_only_for_auto_rows(monkeypatch):
    rows = []
    for route in ({'mode': 'single'}, {'mode': 'team'}, None):
        _, launch = _agent(json.dumps(GOOD), route)
        monkeypatch.setattr(run_bench, 'launch_agent', launch)
        rows.append(run_bench.run_one(TASK, 'auto', 1, 5, sandbox=True))
    _, launch = _agent(json.dumps(GOOD))
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    rows.append(run_bench.run_one(TASK, 'single', 1, 5))
    table = run_bench.summary(rows)
    assert table['billing_qa/auto'] == {'passed': 3, 'lenient_passed': 3, 'runs': 3, 'route_correct': 1,
                                        'routes': {'single': 1, 'team': 1, '?': 1}, 'last_step_passed': 3}
    assert 'route_correct' not in table['billing_qa/single']


def test_code_tasks_expect_the_team_route():
    tasks = {t['id']: t for t in run_bench.load_tasks(['all'])}
    assert tasks['parse_duration']['expected_route'] == tasks['fix_inventory']['expected_route'] == 'team'


def test_dry_run_reports_plans_over_the_cap_and_live_runs_refuse_them(capsys):
    assert run_bench.main(['--tasks', 'all', '--runs', '2']) == 2
    assert 'exceeds --max-runs 6' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run_bench.main(['--tasks', 'all', '--runs', '2', '--confirm-live'])


def test_lenient_grading_takes_the_last_object_of_a_multi_step_answer():
    # Live 2026-11-05: single mode joins every step result, so the JSON came after earlier step text.
    answer = '已读取 billing.py，常量为 {MAX_RETRIES: 3}。\n\n' + json.dumps({'note': 1}) + '\n\n' + json.dumps(GOOD)
    assert summarize(grade_answer_json(answer, EXPECTED)) == (0, 0, False)
    assert summarize(grade_answer_json(answer, EXPECTED, lenient=True)) == (5, 5, True)
    assert summarize(grade_answer_json('没有 JSON', EXPECTED, lenient=True)) == (0, 0, False)


def test_answer_rows_count_json_objects_without_keeping_text(monkeypatch):
    answer = 'step one\n\n' + json.dumps(GOOD)
    _, launch = _agent(answer)
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(TASK, 'single', 1, 5)
    assert row['answer_json_objects'] == 1 and row['answer_chars'] == len(answer)
    assert row['passed'] is False and row['lenient_all_passed'] is True
    assert answer not in json.dumps(row, ensure_ascii=False)


@pytest.mark.parametrize('answer, block', [
    ('读取了文件。' + '\n\n' + json.dumps(GOOD), json.dumps(GOOD)),
    ('说明\n\n```json\n' + json.dumps(GOOD, indent=2) + '\n```\n', '```json\n' + json.dumps(GOOD, indent=2) + '\n```'),
    (json.dumps(GOOD), json.dumps(GOOD)),
    ('最后一步不是 JSON：' + json.dumps(GOOD), None),
    (json.dumps(GOOD) + '\n\n总结完毕', None),
])
def test_last_block_is_the_shortest_trailing_json_object(answer, block):
    from bench.graders import last_block
    assert last_block(answer) == block


def test_answer_rows_compare_the_joined_answer_with_the_last_step(monkeypatch):
    answer = '已读取 billing.py。\n\n' + json.dumps(dict(GOOD, fee_200_60=40)) + '\n\n' + json.dumps(GOOD)
    _, launch = _agent(answer)
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(TASK, 'single', 1, 5)
    assert row['passed'] is False and row['last_step_all_passed'] is True and row['last_step_checks_passed'] == 5
    _, launch = _agent('先说明\n\n' + json.dumps(GOOD) + '\n\n以上')
    monkeypatch.setattr(run_bench, 'launch_agent', launch)
    row = run_bench.run_one(TASK, 'single', 1, 5)
    assert row['last_step_all_passed'] is False and row['lenient_all_passed'] is True
