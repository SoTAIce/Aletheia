"""Run the benchmark against real models: `python -m bench.run_bench --confirm-live ...`.

Every run calls the configured model provider and costs money, so live runs need
--confirm-live and stay under --max-runs (see bench/README.md). Each run gets a
fresh TEMP filesystem root holding only the task's public files; checkers are
never exposed. Web search is disabled for the child process. Only statuses,
counts, exception type names and check results are reported, never model text.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bench.graders import (extract_code, grade_answer_json, grade_html_todo, grade_python, json_objects, last_block,
                           split_cli_output, summarize)

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
# The runtime prefixes planning failures; planner validation messages are host-authored.
_GUARD_REASONS = frozenset({'syntax_error', 'forbidden_import', 'forbidden_name', 'forbidden_attribute'})
_SAFE_FAILURE = re.compile(r'^(Planning failed: )?[A-Za-z]+(Error|Exception)(: [A-Za-z][\w :().,>-]{0,200})?$',
                           re.ASCII)
_TEAM_TRACE = re.compile(r'Team trace: (\{.*\})')
_ROUTE = re.compile(r'Route decision: (\{[^\n]*\})')
_TEAM_COUNTS = re.compile(r'"model_calls": (\d+), "tool_calls": (\d+)')


SPLITS = ('tune', 'holdout')


def load_tasks(names, split='tune'):
    """Tasks by id, or every task of a split for ['all'] (D-035).

    The default split is the tuning set: the held-out set is only run on purpose, so
    improvements are not fitted to it. Tasks named explicitly are returned whatever their split.
    """
    if split not in (*SPLITS, 'all'):
        raise SystemExit(f'unknown split: {split}')
    tasks = {}
    for path in sorted((BENCH / 'tasks').glob('*/task.json')):
        task = json.loads(path.read_text(encoding='utf-8'))
        task['dir'] = path.parent
        tasks[task['id']] = task
    if names == ['all']:
        return [t for t in tasks.values() if split == 'all' or t.get('split', 'tune') == split]
    unknown = [n for n in names if n not in tasks]
    if unknown:
        raise SystemExit(f'unknown task(s): {", ".join(unknown)}')
    return [tasks[n] for n in names]


def grade(task, content):
    if task['grader'] == 'html_todo':
        return grade_html_todo(content)
    if task['grader'] == 'python_checks':
        return grade_python(content, task['dir'] / task['checker'])
    if task['grader'] == 'answer_json':
        return grade_answer_json(content, task['dir'] / task['expected'])
    raise ValueError(f'unknown grader {task["grader"]}')


def _rmtree(path: Path):
    target = str(path)
    if os.name == 'nt' and not target.startswith('\\\\?\\'):
        target = '\\\\?\\' + target
    shutil.rmtree(target, ignore_errors=True)


def _pycache_dirs():
    return {p for p in (REPO / 'app').rglob('__pycache__')}


def _text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value or ''


def launch_agent(cmd, env, timeout):
    """Start one agent process (tests replace this; graders keep using subprocess directly)."""
    return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def _snapshot(root: Path):
    """Names and bytes of every file under the run root; read-only tasks must leave it unchanged."""
    return {str(p.relative_to(root)): (None if p.is_symlink() else p.read_bytes())
            for p in sorted(root.rglob('*')) if p.is_file() or p.is_symlink()}


_RESULT_LIMIT = 5_000_000


def read_result(path: Path):
    """The CLI's --result-json record (D-034), or None when it is missing or malformed."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _RESULT_LIMIT:
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('status'), str) \
            or not isinstance(record.get('content'), str):
        return None
    for name in ('execution_count', 'replan_count'):
        if type(record.get(name)) is not int:
            return None
    return record


def usage_fields(record):
    """Host counts from the result record: failure kind, tokens and tool calls."""
    fields = {}
    if isinstance(record.get('failure_kind'), str):
        fields['failure_kind'] = record['failure_kind']
    usage = record.get('usage')
    if isinstance(usage, dict):
        for name in ('model_calls', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'estimated_cost'):
            value = usage.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                fields[name] = value
        calls = [c for c in usage.get('calls') or () if isinstance(c, dict)]
        if calls:
            # Per provider call: tokens, and the tools the loop's model asked for in reply (loop-trial-001).
            fields['calls'] = [{k: c[k] for k in ('prompt', 'completion', 'requested') if k in c} for c in calls]
            tagged = [c['requested'] for c in calls if isinstance(c.get('requested'), list)]
            if tagged:
                fields['todo_calls'] = sum(1 for names in tagged if 'todo' in names)
                fields['todo_only_calls'] = sum(1 for names in tagged if names and set(names) == {'todo'})
    steps = record.get('steps')
    if isinstance(steps, list):
        tools = [c for s in steps if isinstance(s, dict) for c in s.get('tools') or () if isinstance(c, dict)]
        fields['step_attempts'] = len(steps)
        fields['tool_calls'] = len(tools)
        fields['failed_tool_calls'] = sum(1 for c in tools if c.get('success') is not True)
    return fields


def _read_deliverable(root: Path, name: str, limit: int = 1_000_000):
    """Read the agent-written file as untrusted data: no links, bounded size."""
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        return None
    return path.read_text(encoding='utf-8', errors='replace')


SANDBOX_HINT = ('写完后先写测试，再用 run_tests 或 run_python 在沙盒中运行；根据结果修改，直到运行通过（exit code 0）'
                '后再交付。')


def run_one(task, mode, index, timeout, keep_dir=None, write=False, sandbox=False, team_timeout=None,
            team_acceptance=False, single_runner='loop', trace_dir=None):
    write = write or sandbox
    # Answer tasks are graded on the final answer even when write and sandbox tools are offered;
    # offering them is what lets auto routing consider the team (D-026).
    answer_only = task.get('delivery') == 'answer'
    work = Path(tempfile.mkdtemp(prefix='aletheia-bench-'))
    root = work / 'root'
    root.mkdir()
    try:
        for name in task['files']:
            shutil.copyfile(task['dir'] / name, root / name)
        env = dict(os.environ, TAVILY_API_KEY='', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                   ALETHEIA_TEAM_TRACE='1',
                   CONVERSATION_STORAGE_DIR='')   # no archive: ids differ per run, keep data/ clean
        env.pop('ALETHEIA_TRACE_DIR', None)
        env.pop('ALETHEIA_TRACE_NAME', None)
        if trace_dir is not None:
            # The child writes this run's trace there (TraceFileHook, D-073); the name matches the row.
            env['ALETHEIA_TRACE_DIR'] = str(Path(trace_dir).resolve())
            env['ALETHEIA_TRACE_NAME'] = f'{task["id"]}-{mode}-{index}'
        goal = task['write_goal'] if write and not answer_only else task['goal']
        if sandbox and not answer_only:
            goal += SANDBOX_HINT
        before = _snapshot(root) if answer_only else None
        result_path = work / 'result.json'   # outside the agent's root
        cmd = [sys.executable, '-B', '-m', 'app.agent', goal,
               '--session-id', f'bench-{task["id"]}-{mode}-{index}', '--filesystem-root', str(root),
               '--no-knowledge', '--execution-mode', mode, '--result-json', str(result_path)]
        if write:
            # The TEMP root is both the read and the write root; nothing else is writable.
            cmd += ['--write-root', str(root)]
        if sandbox:
            cmd.append('--sandbox')
        if mode in ('single', 'auto'):
            # Always explicit, so the report's runner is what ran whatever the CLI default is (D-072);
            # auto uses it when it routes to single mode.
            cmd += ['--single-runner', single_runner]
        teamish = mode in ('team', 'auto')   # auto may route to the team (D-026)
        if team_timeout is not None and teamish:
            cmd += ['--team-timeout', str(team_timeout)]
        if teamish and team_acceptance:
            cmd.append('--team-acceptance')
        if team_timeout is not None and teamish:
            # Planning and teardown run outside the team budget; never kill a team that is still in time.
            timeout = max(timeout, team_timeout + 300)
        started = time.monotonic()
        try:
            done = launch_agent(cmd, env, timeout)
            stdout, stderr, timed_out = done.stdout, done.stderr, False
        except subprocess.TimeoutExpired as exc:
            # Keep what the child already logged (the team trace, if the team ended).
            stdout, stderr, timed_out = '', _text(exc.stderr), True
        seconds = round(time.monotonic() - started, 1)
        record = None if timed_out else read_result(result_path)
        if record is not None:
            content, status = record['content'], record['status']
            executions, replans = record['execution_count'], record['replan_count']
        else:
            content, status, executions, replans = split_cli_output(stdout)   # older CLI or no record
        cli_content = content  # failure text comes from the CLI, never from the written file
        row = {'task': task['id'], 'split': task.get('split', 'tune'), 'mode': mode, 'run': index,
               'status': 'timeout' if timed_out else status,
               'executions': executions, 'replans': replans, 'seconds': seconds,
               'result_source': 'result_json' if record is not None else 'stdout'}
        if mode == 'single':
            row['runner'] = single_runner
        if record is not None:
            row.update(usage_fields(record))
        traces = _TEAM_TRACE.findall(stderr)
        if traces:
            parsed = []
            for text in traces:  # host-authored, no model text; one per team step
                try:
                    parsed.append(json.loads(text))
                except ValueError:
                    parsed.append('unparsable')
            row['team_trace'] = parsed[-1]
            if len(parsed) > 1:
                row['team_traces'] = parsed
        route = _ROUTE.search(stderr)
        if route:
            try:
                row['route'] = json.loads(route.group(1))   # host values only (D-026)
            except ValueError:
                row['route'] = 'unparsable'
        if mode == 'auto' and 'expected_route' in task:
            row['route_correct'] = isinstance(row.get('route'), dict) and row['route'].get('mode') == task['expected_route']
        counts = _TEAM_COUNTS.search(stderr)
        if counts:
            row['team_model_calls'], row['team_tool_calls'] = int(counts.group(1)), int(counts.group(2))
        if answer_only:
            row['delivery'] = 'answer'
        elif write:
            row['delivery'] = 'file'
            original = ((task['dir'] / task['deliverable']).read_text(encoding='utf-8')
                        if task['deliverable'] in task['files'] else None)
            written = _read_deliverable(root, task['deliverable'])
            row['deliverable_written'] = written is not None and written != original
            if status == 'completed' and written is None:
                row.update(passed=False, failure='deliverable_missing')
                return row
            content = written if written is not None else content
        if status == 'completed':
            checks = grade(task, content)
            if answer_only:
                checks['workspace_unchanged'] = _snapshot(root) == before
            row['checks_passed'], row['checks_total'], row['passed'] = summarize(checks)
            row['failed_checks'] = sorted(k for k, v in checks.items() if v is not True)
            if checks.get('_reason') in _GUARD_REASONS:
                row['guard_reason'] = checks['_reason']  # host enum: why the static guard refused
            row.update(lenient(task, content))
            if keep_dir is not None:
                suffix = {'html_todo': '.html', 'answer_json': '.json'}.get(task['grader'], '.py')
                Path(keep_dir).mkdir(parents=True, exist_ok=True)
                (Path(keep_dir) / f'{task["id"]}-{mode}-{index}{suffix}').write_text(content, encoding='utf-8')
        else:
            if write and not answer_only and written is not None and written != original:
                # Diagnostic only: how good the file was when the run stopped. Never a pass.
                passed, total, _ = summarize(grade(task, written))
                row['file_checks_passed'], row['file_checks_total'] = passed, total
            first = cli_content.splitlines()[0] if cli_content else ''
            row['failure'] = first if _SAFE_FAILURE.match(first) else ('timeout' if timed_out else 'unrecognized')
            row['passed'] = False
        return row
    finally:
        _rmtree(work)


def lenient(task, content):
    """Diagnostic score after extracting code from prose; never counts as a pass."""
    if task['grader'] == 'answer_json':
        checks = grade_answer_json(content, task['dir'] / task['expected'], lenient=True)
        passed, total, ok = summarize(checks)
        tail = last_block(content)
        last_passed, _, last_ok = summarize(grade(task, tail)) if tail is not None else (0, 0, False)
        # Host counts only, never the text: how many JSON objects the answer held and its length.
        return {'answer_is_code_only': '_parse' not in grade(task, content),
                'last_step_checks_passed': last_passed, 'last_step_all_passed': last_ok,
                'answer_json_objects': len(json_objects(content)), 'answer_chars': len(content),
                'lenient_checks_passed': passed, 'lenient_checks_total': total, 'lenient_all_passed': ok}
    kind = 'html' if task['grader'] == 'html_todo' else 'python'
    code = extract_code(content, kind)
    passed, total, ok = summarize(grade(task, code))
    return {'answer_is_code_only': code.strip() == content.strip(),
            'lenient_checks_passed': passed, 'lenient_checks_total': total, 'lenient_all_passed': ok}


def wilson(passed, runs, z=1.96):
    """95% Wilson score interval for a pass rate, rounded; (0, 1) when nothing ran.

    Runs of different tasks are not identically distributed, so across tasks this is a rough
    guide to sampling noise, not an exact interval (D-035).
    """
    if runs <= 0:
        return [0.0, 1.0]
    rate = passed / runs
    centre = (rate + z * z / (2 * runs)) / (1 + z * z / runs)
    half = z * ((rate * (1 - rate) / runs + z * z / (4 * runs * runs)) ** 0.5) / (1 + z * z / runs)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def split_summary(rows):
    """Pass rate with a 95% interval for each split and mode, over every run in it."""
    table = {}
    for row in rows:
        cell = table.setdefault(f'{row.get("split", "tune")}/{row["mode"]}',
                                {'passed': 0, 'runs': 0, 'tasks': set()})
        cell['runs'] += 1
        cell['passed'] += bool(row.get('passed'))
        cell['tasks'].add(row['task'])
    for cell in table.values():
        cell['tasks'] = len(cell['tasks'])
        cell['pass_rate'] = round(cell['passed'] / cell['runs'], 3)
        cell['ci95'] = wilson(cell['passed'], cell['runs'])
    return table


def summary(rows):
    table = {}
    for row in rows:
        key = f'{row["task"]}/{row["mode"]}'
        cell = table.setdefault(key, {'passed': 0, 'lenient_passed': 0, 'runs': 0})
        cell['runs'] += 1
        cell['passed'] += bool(row.get('passed'))
        cell['lenient_passed'] += bool(row.get('lenient_all_passed'))
        if 'total_tokens' in row:
            cell['total_tokens'] = cell.get('total_tokens', 0) + row['total_tokens']
            cell['model_calls'] = cell.get('model_calls', 0) + row.get('model_calls', 0)
        if 'last_step_all_passed' in row:
            cell['last_step_passed'] = cell.get('last_step_passed', 0) + bool(row['last_step_all_passed'])
        if 'route_correct' in row:
            cell['route_correct'] = cell.get('route_correct', 0) + bool(row['route_correct'])
            routes = cell.setdefault('routes', {})
            chosen = row['route'].get('mode', '?') if isinstance(row.get('route'), dict) else '?'
            routes[chosen] = routes.get(chosen, 0) + 1
    for cell in table.values():
        cell['ci95'] = wilson(cell['passed'], cell['runs'])
    return table


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tasks', nargs='+', default=['all'])
    parser.add_argument('--split', choices=[*SPLITS, 'all'], default='tune',
                        help='which tasks --tasks all means; the held-out set only runs when asked (D-035)')
    parser.add_argument('--modes', nargs='+', choices=['single', 'team', 'auto'], default=['single', 'team'])
    parser.add_argument('--runs', type=int, default=1, help='runs per task and mode')
    parser.add_argument('--max-runs', type=int, default=6, help='refuse plans with more agent runs than this')
    parser.add_argument('--timeout', type=float, default=600, help='seconds per agent run')
    parser.add_argument('--keep-answers', help='directory for completed answers (for manual browser checks)')
    parser.add_argument('--keep-traces', metavar='DIR',
                        help="directory for each run's trace file (read with python -m app.agent.trace FILE)")
    parser.add_argument('--out', help='write the JSON report here as well as to stdout')
    parser.add_argument('--confirm-live', action='store_true', help='required: runs call the real model provider')
    parser.add_argument('--sandbox', action='store_true',
                        help='file delivery plus run_tests/run_python in the Docker sandbox (implies --write)')
    parser.add_argument('--team-timeout', type=float, help='team time budget per run in seconds')
    parser.add_argument('--team-acceptance', action='store_true',
                        help='team V2 + sandbox: opt in to independent acceptance tests (D-022)')
    parser.add_argument('--single-runner', choices=['plan', 'loop'], default='loop',
                        help='how single (and auto-routed single) mode runs: one agent loop (default, D-072), '
                             'or plan then steps')
    parser.add_argument('--write', action='store_true',
                        help='let the agent write the deliverable file into its TEMP root and grade that file')
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error('--runs must be at least 1')
    tasks = load_tasks(args.tasks, args.split)
    if not tasks:
        parser.error(f'no tasks in split {args.split}')
    planned = len(tasks) * len(args.modes) * args.runs
    plan = (f'{planned} agent run(s): tasks={[t["id"] for t in tasks]} modes={args.modes} runs={args.runs} '
            f'splits={sorted({t.get("split", "tune") for t in tasks})} '
            f'team_acceptance={args.team_acceptance} single_runner={args.single_runner} '
            f'delivery={"file" if args.write or args.sandbox else "answer"} sandbox={args.sandbox}')
    if not args.confirm_live:
        over = f' (exceeds --max-runs {args.max_runs}; a live run would be refused)' if planned > args.max_runs else ''
        print('DRY RUN (no model calls). Add --confirm-live to execute ' + plan + over)
        return 2
    if planned > args.max_runs:
        parser.error(f'{plan} exceeds --max-runs {args.max_runs}')
    before = _pycache_dirs()
    rows = []
    try:
        for task in tasks:
            for mode in args.modes:
                for index in range(1, args.runs + 1):
                    rows.append(run_one(task, mode, index, args.timeout, args.keep_answers, args.write,
                                        args.sandbox, args.team_timeout,
                                        args.team_acceptance, args.single_runner, args.keep_traces))
                    print(json.dumps(rows[-1], ensure_ascii=False), file=sys.stderr, flush=True)
    finally:
        # The MCP stdio child does not inherit PYTHONDONTWRITEBYTECODE (DECISIONS D-011).
        for path in _pycache_dirs() - before:
            _rmtree(path)
    report = {'plan': plan, 'rows': rows, 'summary': summary(rows), 'by_split': split_summary(rows)}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
