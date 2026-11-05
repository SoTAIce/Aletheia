"""Deterministic tests for the benchmark graders and runner guard (capability-phase1-001).

Reference answers prove each grader can pass, and known-bad answers prove it can
fail. No model calls: the runner is only exercised in dry-run mode.
"""

from pathlib import Path

import pytest

from bench import run_bench
from bench.graders import (
    extract_code, grade_html_todo, grade_python, split_cli_output, static_guard, strip_fence, summarize,
)

TASKS = Path(__file__).resolve().parents[1] / 'bench' / 'tasks'

GOOD_HTML = '''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>.done span{text-decoration:line-through}</style></head>
<body><h1>Todo</h1><input id="new-todo"><button id="add-btn">添加</button>
<ul id="list"></ul>
<button data-filter="all">全部</button><button data-filter="active">未完成</button>
<button data-filter="completed">已完成</button><span id="remaining-count">0</span>
<script>
let todos = JSON.parse(localStorage.getItem("todos") || "[]");
function save(){ localStorage.setItem("todos", JSON.stringify(todos)); }
function add(){ const v = document.getElementById("new-todo").value.trim(); if(!v) return;
  todos.push({text:v, done:false}); save(); render(); }
document.getElementById("new-todo").addEventListener("keydown", e => { if (e.key === "Enter") add(); });
function render(){ /* li.className = "todo-item"; type = "checkbox"; btn.className = "delete-btn" */ }
</script></body></html>'''

PARSE_DURATION = '''
import re

_TOKEN = re.compile(r"(\\d+)([hms])")

def parse_duration(text):
    if not isinstance(text, str):
        raise TypeError("text must be str")
    parts = text.split()
    if not parts:
        raise ValueError("empty")
    order, seen, total = "hms", -1, 0
    for part in parts:
        pos = 0
        while pos < len(part):
            m = _TOKEN.match(part, pos)
            if not m:
                raise ValueError("bad token")
            rank = order.index(m.group(2))
            if rank <= seen:
                raise ValueError("order")
            seen = rank
            total += int(m.group(1)) * (3600, 60, 1)[rank]
            pos = m.end()
    return total
'''

FIXED_INVENTORY = '''
class Inventory:
    def __init__(self):
        self._items = {}

    def add(self, sku, qty):
        if type(qty) is not int or qty <= 0:
            raise ValueError("qty must be positive")
        self._items[sku] = self._items.get(sku, 0) + qty

    def remove(self, sku, qty):
        if type(qty) is not int or qty <= 0:
            raise ValueError("qty must be positive")
        if self._items.get(sku, 0) < qty:
            raise ValueError("insufficient stock")
        self._items[sku] -= qty
        if self._items[sku] == 0:
            del self._items[sku]

    def count(self, sku):
        return self._items.get(sku, 0)

    def page(self, page, size):
        if page < 1 or size < 1:
            raise ValueError("page and size start at 1")
        skus = sorted(self._items)
        start = (page - 1) * size
        return skus[start:start + size]

    def total_value(self, prices):
        return round(sum(prices[sku] * qty for sku, qty in self._items.items()), 2)
'''


def test_split_cli_output_and_fence():
    content, status, executions, replans = split_cli_output('```html\n<p>x</p>\n```\n\nStatus: completed; executions: 3; replans: 1\n')
    assert (status, executions, replans) == ('completed', 3, 1)
    assert strip_fence(content) == '<p>x</p>'
    assert split_cli_output('crash')[1] is None


def test_html_grader_accepts_reference_and_rejects_external_resources():
    checks = grade_html_todo(GOOD_HTML)
    assert summarize(checks) == (11, 11, True), checks
    bad = GOOD_HTML.replace('<style>', '<script src="https://cdn.example.com/x.js"></script><style>')
    bad = bad.replace('localStorage.getItem("todos"', 'localStorage.getItem("items"')
    failed = {k for k, v in grade_html_todo(bad).items() if not v}
    assert {'no_external_resources', 'localstorage_todos_key'} <= failed


def test_python_grader_passes_reference_solutions():
    passed, total, ok = summarize(grade_python(PARSE_DURATION, TASKS / 'parse_duration' / 'checker.py'))
    assert (passed, total, ok) == (21, 21, True)
    passed, total, ok = summarize(grade_python(FIXED_INVENTORY, TASKS / 'fix_inventory' / 'checker.py'))
    assert (passed, total, ok) == (16, 16, True)


def test_python_grader_fails_the_shipped_buggy_inventory():
    buggy = (TASKS / 'fix_inventory' / 'inventory.py').read_text(encoding='utf-8')
    passed, total, ok = summarize(grade_python(buggy, TASKS / 'fix_inventory' / 'checker.py'))
    assert total == 16 and not ok and passed <= 8


@pytest.mark.parametrize('code, reason', [
    ('import os\n', 'forbidden_import'),
    ('from subprocess import run\n', 'forbidden_import'),
    ('open("x")\n', 'forbidden_name'),
    ('().__class__.__bases__\n', 'forbidden_attribute'),
    ('def f(:\n', 'syntax_error'),
])
def test_static_guard_rejects_unsafe_code(code, reason):
    assert static_guard(code) == reason
    assert summarize(grade_python(code, TASKS / 'parse_duration' / 'checker.py'))[2] is False


def test_static_guard_allows_plain_re_code():
    assert static_guard(PARSE_DURATION) is None


def test_runner_requires_confirmation_and_caps_runs(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('dry run must not start an agent')
    monkeypatch.setattr(run_bench, 'run_one', forbidden)
    assert run_bench.main([]) == 2
    assert 'DRY RUN' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run_bench.main(['--runs', '2', '--confirm-live'])  # 3 tasks x 2 modes x 2 runs > 6
    assert {t['id'] for t in run_bench.load_tasks(['all'])} == {'todo_html', 'parse_duration', 'fix_inventory'}


def test_html_grader_accepts_constant_storage_key():
    html = GOOD_HTML.replace('localStorage.getItem("todos"', 'localStorage.getItem(KEY').replace(
        'localStorage.setItem("todos"', 'localStorage.setItem(KEY').replace('let todos', 'const KEY = "todos";\nlet todos')
    assert grade_html_todo(html)['localstorage_todos_key'] is True
    wrong = html.replace('const KEY = "todos"', 'const KEY = "items"')
    assert grade_html_todo(wrong)['localstorage_todos_key'] is False


def test_extract_code_is_diagnostic_and_strict_score_still_fails_prose():
    prose = '已读取 spec。要求以 <!DOCTYPE html> 开头、以 </html> 结尾。代码如下：\n' + GOOD_HTML + '\n说明：完成。'
    assert summarize(grade_html_todo(prose))[2] is False
    assert summarize(grade_html_todo(extract_code(prose, 'html'))) == (11, 11, True)
    python_prose = '修复如下：\n' + FIXED_INVENTORY.strip() + '\n以上修复了全部缺陷。'
    assert static_guard(python_prose) == 'syntax_error'
    assert static_guard(extract_code(python_prose, 'python')) is None
    assert extract_code('说明\n```python\nx = 1\n```\n结束', 'python') == 'x = 1'


@pytest.mark.parametrize('line, safe', [
    ('StepModelError: Model generation failed: ModelResponseError -> JSONDecodeError', True),
    ('TeamError: team execution failed: ExceptionGroup -> SessionError', True),
    ('StepModelError: leaked "<html>" text', False),
    ('TeamError: team action limit reached after 32 actions', True),
    ('SessionError: model call limit reached (48)', True),
    ("TeamError: leaked 'quoted' text", False),
    ('TeamError: 中文原文', False),
])
def test_failure_line_filter(line, safe):
    assert bool(run_bench._SAFE_FAILURE.match(line)) is safe
