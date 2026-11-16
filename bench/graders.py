"""Deterministic graders for the benchmark (capability-phase1-001).

Graders read only the agent's final answer. Candidate Python code is checked by
an AST guard and then executed in a separate `python -I` process with a timeout
inside a fresh TEMP directory. The guard prevents accidental damage; it is not a
security sandbox, so only run the benchmark on your own machine.
"""

import ast
import json
import math
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


_BEHAVIOUR_SCRIPT = Path(__file__).with_name('todo_behaviour.cjs')
BEHAVIOUR_CHECKS = ('enter_adds_trimmed', 'button_adds', 'blank_not_added', 'complete_strikethrough',
                    'remaining_count_updates', 'filters_show_matching_items', 'persists_after_reload',
                    'delete_removes_item', 'no_horizontal_scroll_375', 'no_script_errors')


def html_behaviour(html: str, timeout: float = 60.0) -> dict | None:
    """The todo page's behaviour in a headless browser (bench/todo_behaviour.cjs), or None when
    node or its Playwright is not available. The page runs from a file in a fresh TEMP directory
    and every other request is aborted."""
    node = shutil.which('node')
    if node is None:
        return None
    with tempfile.TemporaryDirectory(prefix='aletheia-todo-') as directory:
        page = Path(directory) / 'index.html'
        page.write_text(html, encoding='utf-8')
        try:
            done = subprocess.run([node, str(_BEHAVIOUR_SCRIPT), str(page)], capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=timeout, cwd=directory)
            result = json.loads(done.stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
            return None
    checks = result.get('checks') if isinstance(result, dict) else None
    if not isinstance(checks, dict) or set(checks) != set(BEHAVIOUR_CHECKS):
        return None
    return {name: checks[name] is True for name in BEHAVIOUR_CHECKS}


def grade_html_todo(html: str, browser: bool = False) -> dict:
    """Checks for bench/tasks/todo_html/spec.md.

    Static checks look at the source. With browser=True and a browser available, the page is also
    operated (typing, Enter, the button, checkbox, filters, reload, delete, 375 px width), and those
    results replace the static enter_key_and_trim, which refused correct pages that add on Enter
    through a <form> submit (deliverable-quality-001). Without a browser the static check stays.
    """
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
    checks = {
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
    behaviour = html_behaviour(html) if browser else None
    if behaviour is not None:
        del checks['enter_key_and_trim']
        checks.update(behaviour)
    return checks


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
    try:
        # Code that parses as a whole is all code, even when it starts with module constants
        # (business_days was reported as not code-only; deliverable-quality-001).
        ast.parse(text)
        return text
    except SyntaxError:
        pass
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
_ALLOWED_DUNDERS = {
    '__init__', '__name__', '__repr__', '__eq__', '__all__',
    # Harmless data-model names that ordinary solutions use (D-038). Names that reach
    # interpreter internals (__class__, __dict__, __globals__, __builtins__, __subclasses__,
    # __code__, __getattribute__, __import__ ...) stay refused.
    '__slots__', '__hash__', '__version__', '__doc__', '__post_init__',
    '__str__', '__ne__', '__lt__', '__le__', '__gt__', '__ge__', '__bool__',
    '__len__', '__iter__', '__next__', '__contains__', '__getitem__',
}


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


_MISSING = object()


def _same(got, want) -> bool:
    if isinstance(want, bool) or isinstance(got, bool):
        return type(got) is type(want) and got == want
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return math.isclose(got, want, rel_tol=0, abs_tol=1e-9)
    if isinstance(want, list):
        return isinstance(got, list) and len(got) == len(want) and all(map(_same, got, want))
    return got == want


def json_objects(text: str) -> list:
    """Every top-level JSON object embedded in text, in order (diagnostic extraction)."""
    decoder, found, index = json.JSONDecoder(), [], 0
    while (index := text.find('{', index)) >= 0:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = end
    return found


def last_block(text: str):
    """Shortest trailing run of blank-line-separated blocks that is one JSON object (a fence is allowed).

    Single mode joins step results with a blank line, so this approximates 'the final answer is
    only the last step' without changing the runtime. None when no trailing block parses."""
    blocks = text.strip().split('\n\n')
    for count in range(1, len(blocks) + 1):
        tail = '\n\n'.join(blocks[-count:])
        try:
            value = json.loads(strip_fence(tail))
        except ValueError:
            continue
        if isinstance(value, dict):
            return tail
    return None


def grade_answer_json(text: str, expected: Path, lenient: bool = False, also_accept=None) -> dict:
    """One check per expected key. Strict: the answer is one JSON object (a fence is allowed).
    Lenient (diagnostic only): the last JSON object embedded anywhere in the answer.

    also_accept (task.json, hidden from the agent) maps a key to further answers that are equally
    right because the question allows two readings; each case is documented in its task."""
    want = json.loads(expected.read_text(encoding='utf-8'))
    also_accept = also_accept or {}
    if not isinstance(also_accept, dict) or set(also_accept) - set(want) or not all(
            isinstance(v, list) for v in also_accept.values()):
        raise ValueError('also_accept must map expected keys to lists of answers')
    if lenient:
        objects = json_objects(text)
        if not objects:
            return {'_parse': False}
        payload = objects[-1]
    else:
        try:
            payload = json.loads(strip_fence(text))
        except ValueError:
            return {'_parse': False}
    if not isinstance(payload, dict):
        return {'_parse': False}
    return {key: any(_same(payload.get(key, _MISSING), option) for option in (value, *also_accept.get(key, ())))
            for key, value in want.items()}


def summarize(checks: dict) -> tuple[int, int, bool]:
    """(passed, total, all_passed); underscore keys are failures of the grading step itself."""
    passed = sum(1 for k, v in checks.items() if v is True and not k.startswith('_'))
    total = sum(1 for k in checks if not k.startswith('_'))
    return passed, total, total > 0 and passed == total and not any(k.startswith('_') for k in checks)
