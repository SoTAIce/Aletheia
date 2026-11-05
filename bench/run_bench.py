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

from bench.graders import extract_code, grade_html_todo, grade_python, split_cli_output, summarize

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
_SAFE_FAILURE = re.compile(r'^[A-Za-z]+(Error|Exception)(: [A-Za-z][\w :().,>-]{0,200})?$', re.ASCII)
_TEAM_COUNTS = re.compile(r'"model_calls": (\d+), "tool_calls": (\d+)')


def load_tasks(names):
    tasks = {}
    for path in sorted((BENCH / 'tasks').glob('*/task.json')):
        task = json.loads(path.read_text(encoding='utf-8'))
        task['dir'] = path.parent
        tasks[task['id']] = task
    if names == ['all']:
        return list(tasks.values())
    unknown = [n for n in names if n not in tasks]
    if unknown:
        raise SystemExit(f'unknown task(s): {", ".join(unknown)}')
    return [tasks[n] for n in names]


def grade(task, content):
    if task['grader'] == 'html_todo':
        return grade_html_todo(content)
    if task['grader'] == 'python_checks':
        return grade_python(content, task['dir'] / task['checker'])
    raise ValueError(f'unknown grader {task["grader"]}')


def _rmtree(path: Path):
    target = str(path)
    if os.name == 'nt' and not target.startswith('\\\\?\\'):
        target = '\\\\?\\' + target
    shutil.rmtree(target, ignore_errors=True)


def _pycache_dirs():
    return {p for p in (REPO / 'app').rglob('__pycache__')}


def launch_agent(cmd, env, timeout):
    """Start one agent process (tests replace this; graders keep using subprocess directly)."""
    return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def _read_deliverable(root: Path, name: str, limit: int = 1_000_000):
    """Read the agent-written file as untrusted data: no links, bounded size."""
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        return None
    return path.read_text(encoding='utf-8', errors='replace')


SANDBOX_HINT = ('写完后先写测试，再用 run_tests 或 run_python 在沙盒中运行；根据结果修改，直到运行通过（exit code 0）'
                '后再交付。')


def run_one(task, mode, index, timeout, keep_dir=None, write=False, sandbox=False, team_timeout=None):
    write = write or sandbox
    work = Path(tempfile.mkdtemp(prefix='aletheia-bench-'))
    root = work / 'root'
    root.mkdir()
    try:
        for name in task['files']:
            shutil.copyfile(task['dir'] / name, root / name)
        env = dict(os.environ, TAVILY_API_KEY='', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        goal = task['write_goal'] if write else task['goal']
        if sandbox:
            goal += SANDBOX_HINT
        cmd = [sys.executable, '-B', '-m', 'app.agent', goal,
               '--session-id', f'bench-{task["id"]}-{mode}-{index}', '--filesystem-root', str(root),
               '--no-knowledge', '--execution-mode', mode]
        if write:
            # The TEMP root is both the read and the write root; nothing else is writable.
            cmd += ['--write-root', str(root)]
        if sandbox:
            cmd.append('--sandbox')
        if team_timeout is not None and mode == 'team':
            cmd += ['--team-timeout', str(team_timeout)]
        started = time.monotonic()
        try:
            done = launch_agent(cmd, env, timeout)
            stdout, stderr, timed_out = done.stdout, done.stderr, False
        except subprocess.TimeoutExpired:
            stdout, stderr, timed_out = '', '', True
        seconds = round(time.monotonic() - started, 1)
        content, status, executions, replans = split_cli_output(stdout)
        row = {'task': task['id'], 'mode': mode, 'run': index, 'status': 'timeout' if timed_out else status,
               'executions': executions, 'replans': replans, 'seconds': seconds}
        counts = _TEAM_COUNTS.search(stderr)
        if counts:
            row['team_model_calls'], row['team_tool_calls'] = int(counts.group(1)), int(counts.group(2))
        if write:
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
            row['checks_passed'], row['checks_total'], row['passed'] = summarize(checks)
            row['failed_checks'] = sorted(k for k, v in checks.items() if v is not True)
            row.update(lenient(task, content))
            if keep_dir is not None:
                suffix = '.html' if task['grader'] == 'html_todo' else '.py'
                Path(keep_dir).mkdir(parents=True, exist_ok=True)
                (Path(keep_dir) / f'{task["id"]}-{mode}-{index}{suffix}').write_text(content, encoding='utf-8')
        else:
            first = content.splitlines()[0] if content else ''
            row['failure'] = first if _SAFE_FAILURE.match(first) else ('timeout' if timed_out else 'unrecognized')
            row['passed'] = False
        return row
    finally:
        _rmtree(work)


def lenient(task, content):
    """Diagnostic score after extracting code from prose; never counts as a pass."""
    kind = 'html' if task['grader'] == 'html_todo' else 'python'
    code = extract_code(content, kind)
    passed, total, ok = summarize(grade(task, code))
    return {'answer_is_code_only': code.strip() == content.strip(),
            'lenient_checks_passed': passed, 'lenient_checks_total': total, 'lenient_all_passed': ok}


def summary(rows):
    table = {}
    for row in rows:
        key = f'{row["task"]}/{row["mode"]}'
        cell = table.setdefault(key, {'passed': 0, 'lenient_passed': 0, 'runs': 0})
        cell['runs'] += 1
        cell['passed'] += bool(row.get('passed'))
        cell['lenient_passed'] += bool(row.get('lenient_all_passed'))
    return table


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tasks', nargs='+', default=['all'])
    parser.add_argument('--modes', nargs='+', choices=['single', 'team'], default=['single', 'team'])
    parser.add_argument('--runs', type=int, default=1, help='runs per task and mode')
    parser.add_argument('--max-runs', type=int, default=6, help='refuse plans with more agent runs than this')
    parser.add_argument('--timeout', type=float, default=600, help='seconds per agent run')
    parser.add_argument('--keep-answers', help='directory for completed answers (for manual browser checks)')
    parser.add_argument('--out', help='write the JSON report here as well as to stdout')
    parser.add_argument('--confirm-live', action='store_true', help='required: runs call the real model provider')
    parser.add_argument('--sandbox', action='store_true',
                        help='file delivery plus run_tests/run_python in the Docker sandbox (implies --write)')
    parser.add_argument('--team-timeout', type=float, help='team time budget per run in seconds')
    parser.add_argument('--write', action='store_true',
                        help='let the agent write the deliverable file into its TEMP root and grade that file')
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error('--runs must be at least 1')
    tasks = load_tasks(args.tasks)
    planned = len(tasks) * len(args.modes) * args.runs
    plan = (f'{planned} agent run(s): tasks={[t["id"] for t in tasks]} modes={args.modes} runs={args.runs} '
            f'delivery={"file" if args.write or args.sandbox else "answer"} sandbox={args.sandbox}')
    if planned > args.max_runs:
        parser.error(f'{plan} exceeds --max-runs {args.max_runs}')
    if not args.confirm_live:
        print('DRY RUN (no model calls). Add --confirm-live to execute ' + plan)
        return 2
    before = _pycache_dirs()
    rows = []
    try:
        for task in tasks:
            for mode in args.modes:
                for index in range(1, args.runs + 1):
                    rows.append(run_one(task, mode, index, args.timeout, args.keep_answers, args.write,
                                        args.sandbox, args.team_timeout))
                    print(json.dumps(rows[-1], ensure_ascii=False), file=sys.stderr, flush=True)
    finally:
        # The MCP stdio child does not inherit PYTHONDONTWRITEBYTECODE (DECISIONS D-011).
        for path in _pycache_dirs() - before:
            _rmtree(path)
    report = {'plan': plan, 'rows': rows, 'summary': summary(rows)}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
