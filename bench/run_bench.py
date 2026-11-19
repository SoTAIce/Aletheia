"""Run the benchmark against real models: `python -m bench.run_bench --confirm-live ...`.

Every run calls the configured model provider and costs money, so live runs need
--confirm-live and stay under --max-runs (see bench/README.md). Each run gets a
fresh TEMP filesystem root holding only the task's public files; checkers are
never exposed. Each run also gets its own conversation archive next to that root,
as the product has by default (D-086; --no-archive for the control). Web search is
disabled for the child process. Only statuses, counts, exception type names and
check results are reported, never model text.
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
_ROUTE = re.compile(r'Route decision: (\{[^\n]*\})')


SPLITS = ('tune', 'holdout', 'probe')
# Probe tasks check that delegation is used when it should be (loop-delegate-001); --split all
# leaves them out, so the tuning and held-out numbers stay comparable.
_ALL_SPLITS = ('tune', 'holdout')


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
        return [t for t in tasks.values() if (t.get('split', 'tune') in _ALL_SPLITS if split == 'all'
                                              else t.get('split', 'tune') == split)]
    unknown = [n for n in names if n not in tasks]
    if unknown:
        raise SystemExit(f'unknown task(s): {", ".join(unknown)}')
    return [tasks[n] for n in names]


def grade(task, content):
    if task['grader'] == 'html_todo':
        return grade_html_todo(content, browser=True)
    if task['grader'] == 'python_checks':
        return grade_python(content, task['dir'] / task['checker'])
    if task['grader'] == 'answer_json':
        return grade_answer_json(content, task['dir'] / task['expected'], also_accept=task.get('also_accept'))
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
    outside = record.get('auxiliary_outside_requests')
    if isinstance(outside, dict) and outside:
        fields['auxiliary_outside_requests'] = {key: dict(counts) for key, counts in outside.items()
                                                if isinstance(key, str) and isinstance(counts, dict)}
    usage = record.get('usage')
    if isinstance(usage, dict):
        for name in ('model_calls', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'estimated_cost'):
            value = usage.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                fields[name] = value
        calls = [c for c in usage.get('calls') or () if isinstance(c, dict)]
        if calls:
            # Per provider call: tokens, and the tools the loop's model asked for in reply (loop-trial-001).
            fields['calls'] = [{k: c[k] for k in ('prompt', 'completion', 'requested', 'member', 'profile', 'purpose')
                                if k in c} for c in calls]
            tagged = [c['requested'] for c in calls if isinstance(c.get('requested'), list)]
            if tagged:
                fields['todo_calls'] = sum(1 for names in tagged if 'todo' in names)
                fields['todo_only_calls'] = sum(1 for names in tagged if names and set(names) == {'todo'})
            members = [c.get('member') for c in calls if isinstance(c.get('member'), str)]
            requested = sum(c['requested'].count('delegate') for c in calls
                            if isinstance(c.get('requested'), list) and not isinstance(c.get('member'), str))
            if members or requested:
                # Started members, the loop's delegate requests (refused ones too) and member calls.
                fields['delegations'] = len(set(members))
                fields['delegate_requests'] = requested
                fields['delegate_calls'] = len(members)
        by_profile = usage.get('by_profile')
        if isinstance(by_profile, dict) and len(by_profile) > 1:
            # Usage per model profile when roles ran on different models (stage 4d-2).
            fields['by_profile'] = {name: dict(counts) for name, counts in by_profile.items()
                                    if isinstance(name, str) and isinstance(counts, dict)}
        for name in ('by_purpose', 'auxiliary'):
            # Where the run's tokens went (docs/cost_measurement_plan.md).
            table = usage.get(name)
            if isinstance(table, dict) and table:
                fields[name] = {key: dict(counts) for key, counts in table.items()
                                if isinstance(key, str) and isinstance(counts, dict)}
        provider = usage.get('provider')
        if isinstance(provider, dict):
            # What the provider did to the requests (provider-state-001): first call's band, all bands,
            # reported/estimated ratio per call, and whether it named another model.
            from app.agent.provider_state import STATES
            state = provider.get('state')
            fields['provider_state'] = state if state in STATES else 'unknown'
            fields['provider_states'] = [x for x in provider.get('states') or () if x in STATES]
            fields['provider_ratios'] = [x if isinstance(x, (int, float)) and not isinstance(x, bool) else None
                                         for x in provider.get('ratios') or ()]
            fields['model_mismatch'] = provider.get('model_mismatch') is True
    steps = record.get('steps')
    if isinstance(steps, list):
        tools = [c for s in steps if isinstance(s, dict) for c in s.get('tools') or () if isinstance(c, dict)]
        fields['step_attempts'] = len(steps)
        fields['tool_calls'] = len(tools)
        fields['failed_tool_calls'] = sum(1 for c in tools if c.get('success') is not True)
        # Reads of a saved tool result back from the archive (D-052, D-086).
        fields['artifact_reads'] = sum(1 for c in tools if c.get('tool') == 'read_context_artifact')
        if any(isinstance(s, dict) and s.get('unverified') is True for s in steps):
            fields['unverified'] = True   # answered after changing files with no passing run (D-087)
    return fields


def archived_results(archive: Path) -> int:
    """Tool results this run saved to its archive instead of sending them whole (D-052); a count only."""
    return sum(1 for p in archive.glob('session-*/artifacts/*') if p.is_file() and not p.is_symlink())


# Model errors that are the model's own doing, not the provider's: output past the limit, and
# tool arguments still wrong after the repairs (D-086).
_AGENT_MODEL_ERRORS = ('ModelOutputLimitError', 'ModelToolArgumentsError')


def attribute(row):
    """Who a failed run is put down to, from host fields only (D-086); None for a pass.

    provider: the provider still failed after the retries; grader: grading itself raised;
    unknown: the bench killed the run, or its failure line was not recognised; agent: the rest.
    """
    if row.get('passed'):
        return None
    if 'grader_error' in row:
        return 'grader'
    if row.get('status') == 'timeout' or row.get('failure') == 'unrecognized':
        return 'unknown'
    if row.get('failure_kind') == 'model' and not any(name in (row.get('failure') or '')
                                                      for name in _AGENT_MODEL_ERRORS):
        return 'provider'
    return 'agent'


def _finish(row):
    if not row.get('passed'):
        row['attribution'] = attribute(row)
    return row


def _read_deliverable(root: Path, name: str, limit: int = 1_000_000):
    """Read the agent-written file as untrusted data: no links, bounded size."""
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        return None
    return path.read_text(encoding='utf-8', errors='replace')


SANDBOX_HINT = ('写完后先写测试，再用 run_tests 或 run_python 在沙盒中运行；根据结果修改，直到运行通过（exit code 0）'
                '后再交付。')


def run_one(task, mode, index, timeout, keep_dir=None, write=False, sandbox=False, team_timeout=None,
            single_runner='loop', trace_dir=None, delegate=False, archive=True):
    write = write or sandbox
    # Answer tasks are graded on the final answer even when write and sandbox tools are offered;
    # offering them is what lets auto routing consider the team (D-026).
    answer_only = task.get('delivery') == 'answer'
    work = Path(tempfile.mkdtemp(prefix='aletheia-bench-'))
    root = work / 'root'
    root.mkdir()
    # The run's own archive, a sibling of the root so the agent's file tools never see it; it goes
    # with the work directory. Without it the child runs with no archive at all (D-086).
    archive_dir = work / 'archive'
    if archive:
        archive_dir.mkdir()
    try:
        for name in task['files']:
            # Probe tasks keep files in subdirectories (relative, '/'-separated, checked by the tests).
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(task['dir'] / name, root / name)
        env = dict(os.environ, TAVILY_API_KEY='', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                   ALETHEIA_TEAM_TRACE='1',
                   CONVERSATION_STORAGE_DIR=str(archive_dir) if archive else '',
                   # The user's own personas must not change a bench run (docs/persona_plan.md).
                   PERSONAS_DIR='')
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
               '--no-knowledge', '--execution-mode', mode, '--result-json', str(result_path),
               # The root is the bench's own new folder under TEMP, which is on C: (D-074).
               '--confirm-system-drive']
        if write:
            # The TEMP root is both the read and the write root; nothing else is writable.
            cmd += ['--write-root', str(root)]
        if sandbox:
            cmd.append('--sandbox')
        if mode in ('single', 'auto'):
            # Always explicit, so the report's runner is what ran whatever the CLI default is (D-072);
            # auto uses it when it routes to single mode.
            cmd += ['--single-runner', single_runner]
            if delegate:
                cmd.append('--delegate')
        teamish = mode in ('team', 'auto')   # auto may route to the team (D-026)
        if team_timeout is not None and teamish:
            cmd += ['--team-timeout', str(team_timeout)]
        if team_timeout is not None and teamish:
            # Planning and teardown run outside the team budget; never kill a team that is still in time.
            timeout = max(timeout, team_timeout + 300)
        started = time.monotonic()
        try:
            done = launch_agent(cmd, env, timeout)
            stdout, stderr, timed_out = done.stdout, done.stderr, False
        except subprocess.TimeoutExpired as exc:
            # Keep partial stderr for route diagnostics.
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
               'result_source': 'result_json' if record is not None else 'stdout',
               'archive': archive, 'archived_tool_results': archived_results(archive_dir) if archive else 0}
        if mode == 'single':
            row['runner'] = single_runner
        if delegate and mode in ('single', 'auto'):
            row['delegate'] = True
        if record is not None:
            row.update(usage_fields(record))
        route = _ROUTE.search(stderr)
        if route:
            try:
                row['route'] = json.loads(route.group(1))   # host values only (D-026)
            except ValueError:
                row['route'] = 'unparsable'
        if mode == 'auto' and 'expected_route' in task:
            row['route_correct'] = isinstance(row.get('route'), dict) and row['route'].get('mode') == task['expected_route']
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
                return _finish(row)
            content = written if written is not None else content
        if status == 'completed':
            try:
                checks = grade(task, content)
            except Exception as exc:   # a broken grader fails this row, not the whole bench (D-086)
                row.update(passed=False, grader_error=type(exc).__name__)
                return _finish(row)
            if answer_only:
                checks['workspace_unchanged'] = _snapshot(root) == before
            row['checks_passed'], row['checks_total'], row['passed'] = summarize(checks)
            row['failed_checks'] = sorted(k for k, v in checks.items() if v is not True)
            if task['grader'] == 'html_todo':
                # Which kind of checks graded the page: in a browser, or the source text only.
                row['html_checks'] = 'browser' if 'enter_adds_trimmed' in checks else 'static'
            if checks.get('_reason') in _GUARD_REASONS:
                row['guard_reason'] = checks['_reason']  # host enum: why the static guard refused
            try:
                row.update(lenient(task, content))
            except Exception as exc:   # diagnostics only; the strict result above stands
                row['lenient_error'] = type(exc).__name__
            if keep_dir is not None:
                suffix = {'html_todo': '.html', 'answer_json': '.json'}.get(task['grader'], '.py')
                Path(keep_dir).mkdir(parents=True, exist_ok=True)
                (Path(keep_dir) / f'{task["id"]}-{mode}-{index}{suffix}').write_text(content, encoding='utf-8')
        else:
            if write and not answer_only and written is not None and written != original:
                # Diagnostic only: how good the file was when the run stopped. Never a pass.
                try:
                    passed, total, _ = summarize(grade(task, written))
                    row['file_checks_passed'], row['file_checks_total'] = passed, total
                except Exception as exc:
                    row['file_checks_error'] = type(exc).__name__
            first = cli_content.splitlines()[0] if cli_content else ''
            row['failure'] = first if _SAFE_FAILURE.match(first) else ('timeout' if timed_out else 'unrecognized')
            row['passed'] = False
        return _finish(row)
    finally:
        _rmtree(work)


def lenient(task, content):
    """Diagnostic score after extracting code from prose; never counts as a pass."""
    if task['grader'] == 'answer_json':
        checks = grade_answer_json(content, task['dir'] / task['expected'], lenient=True,
                                   also_accept=task.get('also_accept'))
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


_EXCLUDED = ('provider', 'grader')   # failures not counted against the agent (D-086)


def split_summary(rows):
    """Pass rate with a 95% interval for each split and mode, over every run in it.

    Also (D-086): how many tasks passed every one of their runs, and the agent's pass rate once
    runs that failed on the provider or the grader are left out of the denominator.
    """
    table, per_task = {}, {}
    for row in rows:
        key = f'{row.get("split", "tune")}/{row["mode"]}'
        cell = table.setdefault(key, {'passed': 0, 'runs': 0, 'tasks': set(),
                                      'excluded': dict.fromkeys(_EXCLUDED, 0)})
        cell['runs'] += 1
        cell['passed'] += bool(row.get('passed'))
        cell['tasks'].add(row['task'])
        if row.get('attribution') in _EXCLUDED:
            cell['excluded'][row['attribution']] += 1
        counts = per_task.setdefault(key, {}).setdefault(row['task'], [0, 0])
        counts[0] += bool(row.get('passed'))
        counts[1] += 1
    for key, cell in table.items():
        cell['tasks'] = len(cell['tasks'])
        cell['pass_rate'] = round(cell['passed'] / cell['runs'], 3)
        cell['ci95'] = wilson(cell['passed'], cell['runs'])
        runs = [n for _, n in per_task[key].values()]
        cell['runs_per_task'] = [min(runs), max(runs)]
        cell['tasks_all_passed'] = sum(1 for passed, n in per_task[key].values() if passed == n)
        cell['all_passed_rate'] = round(cell['tasks_all_passed'] / cell['tasks'], 3)
        cell['agent_runs'] = cell['runs'] - sum(cell['excluded'].values())
        cell['agent_passed'] = cell['passed']
        cell['agent_pass_rate'] = (round(cell['agent_passed'] / cell['agent_runs'], 3)
                                   if cell['agent_runs'] else None)
        cell['agent_ci95'] = wilson(cell['agent_passed'], cell['agent_runs'])
    return table


def provider_state_counts(rows):
    """Runs per provider state of their first call, over the whole report (D-079, D-086)."""
    from app.agent.provider_state import STATES
    counts = dict.fromkeys(STATES, 0)
    for row in rows:
        state = row.get('provider_state')
        counts[state if state in STATES else 'unknown'] += 1
    return counts


_KEY = re.compile(r'[a-z]{1,20}/[a-z]{1,20}')


def _percent(value):
    return f'{round(100 * value)}%'


def headline(rows, by_split):
    """One Chinese line per split and mode, built by the host from numbers only (D-086)."""
    from app.agent.provider_state import STATES
    lines = []
    for key, cell in by_split.items():
        mine = [row for row in rows if f'{row.get("split", "tune")}/{row["mode"]}' == key]
        low, high = cell['ci95']
        text = (f'{key if _KEY.fullmatch(key) else "?"}：{cell["runs"]} 次运行通过 {cell["passed"]} 次'
                f'（{_percent(cell["pass_rate"])}，95% 区间 {_percent(low)}～{_percent(high)}）；')
        fewest, most = cell['runs_per_task']
        if fewest == most:
            text += f'每题 {fewest} 次，全过 {cell["tasks_all_passed"]}/{cell["tasks"]} 题；'
        else:
            text += (f'每题 {fewest}～{most} 次，全过 {cell["tasks_all_passed"]}/{cell["tasks"]} 题'
                     '（次数不一致，全过比例仅供参考）；')
        excluded = cell['excluded']
        text += f'不计服务商失败 {excluded["provider"]} 次、评分失败 {excluded["grader"]} 次后'
        if cell['agent_runs']:
            agent_low, agent_high = cell['agent_ci95']
            text += (f'为 {cell["agent_passed"]}/{cell["agent_runs"]}（{_percent(cell["agent_pass_rate"])}，'
                     f'区间 {_percent(agent_low)}～{_percent(agent_high)}）；')
        else:
            text += '没有可计的运行；'
        states = provider_state_counts(mine)
        text += '服务商状态 ' + '、'.join(f'{state} {states[state]}' for state in STATES if states[state])
        mismatched = sum(1 for row in mine if row.get('model_mismatch') is True)
        if mismatched:
            text += f'；回答模型和请求不同 {mismatched} 次'
        lines.append(text + '。')
    return lines


def state_summary(rows):
    """Pass counts by the provider state of each run's first call (provider-state-001): the relay's
    hidden additions changed results, so compare runs only within one state."""
    table = {}
    for row in rows:
        cell = table.setdefault(f'{row.get("provider_state", "unknown")}/{row["mode"]}',
                                {'passed': 0, 'runs': 0, 'tasks': []})
        cell['runs'] += 1
        cell['passed'] += bool(row.get('passed'))
        cell['tasks'].append(row['task'])
    for cell in table.values():
        cell['ci95'] = wilson(cell['passed'], cell['runs'])
    return table


def summary(rows):
    table = {}
    for row in rows:
        key = f'{row["task"]}/{row["mode"]}'
        cell = table.setdefault(key, {'passed': 0, 'lenient_passed': 0, 'runs': 0,
                                      'provider_failures': 0, 'grader_failures': 0})
        cell['runs'] += 1
        cell['passed'] += bool(row.get('passed'))
        cell['lenient_passed'] += bool(row.get('lenient_all_passed'))
        if row.get('attribution') in _EXCLUDED:
            cell[f'{row["attribution"]}_failures'] += 1
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
        cell['all_passed'] = cell['passed'] == cell['runs']
        cell['ci95'] = wilson(cell['passed'], cell['runs'])
    return table


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def purpose_summary(rows):
    """Every run's usage added up by purpose, with each purpose's share of the tokens and of the
    estimated cost; calls outside the agent model separately (docs/cost_measurement_plan.md).
    A purpose's cost is None when any of its calls had no price."""
    model, other = {}, {}
    for row in rows:
        for name, counts in (row.get('by_purpose') or {}).items():
            cell = model.setdefault(name, {'model_calls': 0, 'tokens': 0, 'cached_prompt_tokens': 0,
                                           'estimated_cost': 0.0})
            cell['model_calls'] += _number(counts.get('model_calls'))
            cell['tokens'] += _number(counts.get('prompt_tokens')) + _number(counts.get('completion_tokens'))
            cell['cached_prompt_tokens'] += _number(counts.get('cached_prompt_tokens'))
            cost = counts.get('estimated_cost')
            if cell['estimated_cost'] is not None:
                cell['estimated_cost'] = (None if not isinstance(cost, (int, float)) or isinstance(cost, bool)
                                          else cell['estimated_cost'] + cost)
        for table in (row.get('auxiliary') or {}, row.get('auxiliary_outside_requests') or {}):
            for name, counts in table.items():
                cell = other.setdefault(name, {'calls': 0, 'calls_without_usage': 0, 'tokens': 0})
                cell['calls'] += _number(counts.get('calls'))
                cell['calls_without_usage'] += _number(counts.get('calls_without_usage'))
                cell['tokens'] += _number(counts.get('prompt_tokens')) + _number(counts.get('completion_tokens'))
    tokens = sum(cell['tokens'] for cell in model.values())
    costs = [cell['estimated_cost'] for cell in model.values()]
    cost = None if None in costs else sum(costs)
    for cell in model.values():
        cell['token_share'] = round(cell['tokens'] / tokens, 3) if tokens else None
        cell['cost_share'] = (round(cell['estimated_cost'] / cost, 3)
                              if cost and cell['estimated_cost'] is not None else None)
        if cell['estimated_cost'] is not None:
            cell['estimated_cost'] = round(cell['estimated_cost'], 6)
    return {'model': model, 'other': other, 'tokens': tokens,
            'estimated_cost': None if cost is None else round(cost, 6)}


_PURPOSE_NAMES = {'step': '主循环', 'route': '路由', 'wrap_up': '收尾', 'lead': '团队负责人', 'member': '团队成员',
                  'reviewer': '审查员', 'explorer': '调研成员', 'query_rewrite': '查询改写', 'embedding': '嵌入',
                  'summary': '历史摘要'}


def purpose_headline(table):
    """One Chinese line per kind: the agent model's purposes by share, then the other calls."""
    lines = []
    if table['model']:
        parts = []
        for name, cell in sorted(table['model'].items(), key=lambda item: -item[1]['tokens']):
            share = '?' if cell['token_share'] is None else _percent(cell['token_share'])
            text = f"{_PURPOSE_NAMES.get(name, name)} {cell['model_calls']} 次，令牌 {share}"
            if cell['cost_share'] is not None:
                text += f"，费用 {_percent(cell['cost_share'])}"
            parts.append(text)
        lines.append('按用途（共 ' + f"{table['tokens']:,} 令牌）：" + '；'.join(parts))
    if table['other']:
        lines.append('其他调用：' + '；'.join(
            f"{_PURPOSE_NAMES.get(name, name)} {cell['calls']} 次 {cell['tokens']:,} 令牌"
            for name, cell in table['other'].items()))
    return lines


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
    parser.add_argument('--single-runner', choices=['loop'], default='loop',
                        help='how single (and auto-routed single) mode runs: one agent loop (D-072); '
                             'plan mode was removed (D-076)')
    parser.add_argument('--delegate', action='store_true',
                        help='let the single loop delegate read-only research (loop-delegate-001)')
    parser.add_argument('--write', action='store_true',
                        help='let the agent write the deliverable file into its TEMP root and grade that file')
    parser.add_argument('--no-archive', action='store_true',
                        help='run without a conversation archive (the control; by default each run has its own, D-086)')
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error('--runs must be at least 1')
    tasks = load_tasks(args.tasks, args.split)
    if not tasks:
        parser.error(f'no tasks in split {args.split}')
    planned = len(tasks) * len(args.modes) * args.runs
    plan = (f'{planned} agent run(s): tasks={[t["id"] for t in tasks]} modes={args.modes} runs={args.runs} '
            f'splits={sorted({t.get("split", "tune") for t in tasks})} '
            f'single_runner={args.single_runner} delegate={args.delegate} '
            f'delivery={"file" if args.write or args.sandbox else "answer"} sandbox={args.sandbox} '
            f'archive={"off" if args.no_archive else "on"}')
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
                                        args.single_runner, args.keep_traces, args.delegate,
                                        archive=not args.no_archive))
                    print(json.dumps(rows[-1], ensure_ascii=False), file=sys.stderr, flush=True)
    finally:
        # The MCP stdio child does not inherit PYTHONDONTWRITEBYTECODE (DECISIONS D-011).
        for path in _pycache_dirs() - before:
            _rmtree(path)
    by_split = split_summary(rows)
    report = {'plan': plan, 'headline': headline(rows, by_split), 'rows': rows, 'summary': summary(rows),
              'by_split': by_split, 'by_provider_state': state_summary(rows),
              'provider_state_counts': provider_state_counts(rows),
              'model_mismatch_runs': sum(1 for row in rows if row.get('model_mismatch')),
              'by_purpose': purpose_summary(rows)}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    for line in report['headline'] + purpose_headline(report['by_purpose']):
        print(line, file=sys.stderr)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
