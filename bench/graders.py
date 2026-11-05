"""Deterministic graders for the benchmark (capability-phase1-001).

Graders read only the agent's final answer. Candidate Python code is checked by
an AST guard and then executed in a separate `python -I` process with a timeout
inside a fresh TEMP directory. The guard prevents accidental damage; it is not a
security sandbox, so only run the benchmark on your own machine.
"""

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

_TRAILER = re.compile(r'\n*Status: (\w+); executions: (\d+); replans: (\d+)\s*$')
_FENCE = re.compile(r'^\s*```[\w-]*[^\n]*\n(.*?)\n?```\s*$', re.S)


def split_cli_output(stdout: str):
    """Return (content, status, executions, replans) from `python -m app.agent` stdout."""
    match = _TRAILER.search(stdout)
    if not match:
        return stdout.strip(), None, None, None
    return (stdout[:match.start()].strip(), match.group(1),
            int(match.group(2)), int(match.group(3)))


def strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    return (match.group(1) if match else text).strip()


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.filters, self.metas, self.urls = set(), set(), [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if attrs.get('data-filter'):
            self.filters.add(attrs['data-filter'])
        if tag == 'meta':
            self.metas.append(attrs)
        for name in ('src', 'href'):
            if attrs.get(name):
                self.urls.append(attrs[name])


_EXTERNAL = re.compile(r'^\s*(https?:)?//', re.I)


def grade_html_todo(html: str) -> dict:
    """Static checks for bench/tasks/todo_html/spec.md; behaviour is checked by hand in a browser."""
    html = strip_fence(html)
    parser = _Collector()
    try:
        parser.feed(html)
        parser.close()
        parsed = True
    except Exception:
        parsed = False
    viewport = any(m.get('name') == 'viewport' and 'width=device-width' in (m.get('content') or '')
                   for m in parser.metas)
    return {
        'parses': parsed,
        'doctype_and_close': bool(re.match(r'\s*<!doctype html', html, re.I)) and html.rstrip().lower().endswith('</html>'),
        'no_external_resources': not any(_EXTERNAL.match(u) for u in parser.urls)
                                 and not re.search(r'@import|url\(\s*[\'"]?(https?:)?//', html, re.I),
        'input_and_button_ids': {'new-todo', 'add-btn'} <= parser.ids,
        'remaining_count_id': 'remaining-count' in parser.ids,
        'three_filters': {'all', 'active', 'completed'} <= parser.filters,
        'item_and_delete_classes': 'todo-item' in html and 'delete-btn' in html,
        'checkbox_and_strikethrough': 'checkbox' in html and 'line-through' in html,
        'enter_key_and_trim': bool(re.search(r'[\'"]Enter[\'"]|keyCode\s*===?\s*13', html)) and '.trim()' in html,
        'localstorage_todos_key': all(_uses_todos_key(html, op) for op in ('getItem', 'setItem')),
        'viewport_meta': viewport,
    }


def _uses_todos_key(html: str, op: str) -> bool:
    """localStorage.<op>("todos") directly or through a constant assigned "todos"."""
    for arg in re.findall(r'localStorage\.' + op + r'\(\s*([^,)\s]+)', html):
        if re.fullmatch(r'[\'"]todos[\'"]', arg):
            return True
        if re.fullmatch(r'[A-Za-z_$][\w$]*', arg) and re.search(
                r'\b' + re.escape(arg) + r'\s*=\s*[\'"]todos[\'"]', html):
            return True
    return False


def extract_code(text: str, kind: str) -> str:
    """Diagnostic only: pull the code out of an answer that also contains prose."""
    if kind == 'html':
        # Prose often quotes "<!DOCTYPE html> ... </html>" first; the document is the last one.
        lower = text.lower()
        start, end = lower.rfind('<!doctype html'), lower.rfind('</html>')
        return text[start:end + len('</html>')] if 0 <= start < end else text
    blocks = re.findall(r'```[\w-]*[^\n]*\n(.*?)```', text, re.S)
    if blocks:
        return max(blocks, key=len).strip()
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(r'(import |from |class |def |@)', line)), None)
    if start is None:
        return text
    for end in range(len(lines), start, -1):
        candidate = '\n'.join(lines[start:end])
        try:
            ast.parse(candidate)
            return candidate
        except SyntaxError:
            continue
    return text


_ALLOWED_IMPORTS ={'re', 'math', 'typing', 'dataclasses', 'collections', 'functools', 'itertools', 'string'}
_BANNED_NAMES = {'open', 'exec', 'eval', 'compile', '__import__', 'input', 'breakpoint'}
_ALLOWED_DUNDERS = {'__init__', '__name__', '__repr__', '__eq__', '__all__'}


def static_guard(code: str):
    """Return None when the code may be executed, else a short rejection reason."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return 'syntax_error'
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split('.')[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or '').split('.')[0]] if node.level == 0 else ['<relative>']
        else:
            names = []
        if any(n not in _ALLOWED_IMPORTS for n in names):
            return 'forbidden_import'
        if isinstance(node, ast.Name) and (node.id in _BANNED_NAMES or (
                node.id.startswith('__') and node.id not in _ALLOWED_DUNDERS)):
            return 'forbidden_name'
        if isinstance(node, ast.Attribute) and node.attr.startswith('__') and node.attr not in _ALLOWED_DUNDERS:
            return 'forbidden_attribute'
    return None


_HARNESS = r'''
import importlib.util, json, sys

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

results = {}
try:
    solution = load("solution", sys.argv[1])
    checker = load("checker", sys.argv[2])
    for name, check in checker.checks(solution):
        try:
            results[name] = bool(check())
        except Exception:
            results[name] = False
except Exception as exc:
    results = {"_load": False, "_error_type": type(exc).__name__}
print(json.dumps(results))
'''


def grade_python(code: str, checker: Path, timeout: float = 10.0) -> dict:
    code = strip_fence(code)
    reason = static_guard(code)
    if reason:
        return {'_guard': False, '_reason': reason}
    work = Path(tempfile.mkdtemp(prefix='aletheia-grade-'))
    try:
        (work / 'solution.py').write_text(code, encoding='utf-8')
        (work / 'harness.py').write_text(_HARNESS, encoding='utf-8')
        try:
            done = subprocess.run(
                [sys.executable, '-I', '-B', str(work / 'harness.py'), str(work / 'solution.py'), str(checker)],
                cwd=work, capture_output=True, text=True, encoding='utf-8', timeout=timeout)
        except subprocess.TimeoutExpired:
            return {'_timeout': False}
        lines = done.stdout.strip().splitlines()
        try:
            return json.loads(lines[-1]) if lines else {'_no_output': False}
        except ValueError:
            return {'_bad_output': False}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def summarize(checks: dict) -> tuple[int, int, bool]:
    """(passed, total, all_passed); underscore keys are failures of the grading step itself."""
    passed = sum(1 for k, v in checks.items() if v is True and not k.startswith('_'))
    total = sum(1 for k in checks if not k.startswith('_'))
    return passed, total, total > 0 and passed == total and not any(k.startswith('_') for k in checks)
