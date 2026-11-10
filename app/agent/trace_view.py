"""Traces from audit records (trace-001, D-073): the events of one run become a tree of spans.

Pure functions: the input is the records of one audit file (the header first), as written by
AuditHook or TraceFileHook; nothing is read from disk and no service is called. Version 2
records carry span ids (hooks.trace_scope); version 1 records have none, so their model and
tool calls are paired in order and listed flat under the task ('paired_by_order').

Only host-authored fields are kept. The loop's todo items (model text) are reduced to counts.
"""

from __future__ import annotations

KINDS = ('task', 'route', 'plan', 'step', 'member', 'model', 'tool', 'todo')
_MODEL_ATTRS = ('message_count', 'tool_count', 'callable_tools', 'provider_calls', 'prompt_tokens',
                'completion_tokens', 'cached_tokens', 'tool_calls', 'response_model', 'denied_by', 'error')
_TOOL_ATTRS = ('call_id', 'side_effect', 'args_summary', 'success', 'chars', 'denied_by', 'error')
_TASK_ATTRS = ('failure_kind', 'model_calls', 'total_tokens', 'cached_prompt_tokens', 'error')
_STEP_ATTRS = ('outcome', 'runner', 'step_id')
_LABELS = ('index', 'revision', 'member')


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _span(span_id, parent, kind, label, start, **attrs) -> dict:
    return {'id': span_id, 'parent': parent, 'kind': kind, 'label': label, 'start': start,
            'seconds': None, 'status': 'unfinished', 'attrs': {k: v for k, v in attrs.items() if v is not None},
            'children': []}


def _copy(event: dict, keys) -> dict:
    return {key: event[key] for key in keys if event.get(key) is not None}


def _call_status(event: dict) -> str:
    if event.get('denied_by'):
        return 'denied'
    if event.get('error'):
        return 'error'
    return 'ok' if event.get('success') else 'failed'


def _finish_call(span: dict, event: dict, keys) -> None:
    span['seconds'] = _number(event.get('seconds'))
    if span['seconds'] is None and event.get('denied_by'):
        span['seconds'] = 0.0
    span['status'] = _call_status(event)
    span['attrs'].update(_copy(event, keys))


def _todo_attrs(event: dict) -> dict:
    items = event.get('items') if isinstance(event.get('items'), list) else []
    done = sum(1 for item in items if isinstance(item, dict) and item.get('status') == 'done')
    return {'items': len(items), 'done': done}


def build_trace(records: list[dict]) -> dict:
    """The trace of one run: header facts, totals and the span tree (see docs/tracing_plan.md 4.4)."""
    records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
    header = records[0] if records and records[0].get('type') == 'audit' else {}
    events = records[1:] if header else records
    has_spans = any('span_id' in e for e in events if e.get('type') in ('before_model', 'before_tool', 'task_start'))
    version = 2 if has_spans else 1
    spans: dict[str, dict] = {}
    order: list[dict] = []
    root = None
    task_end = None
    pending: dict[str, list] = {'model': [], 'tool': []}     # version 1: unmatched calls, in order
    numbers = iter(range(1, 1_000_000))

    def add(span):
        spans[span['id']] = span
        order.append(span)
        return span

    def ensure_root(t=0.0):
        nonlocal root
        if root is None:
            root = add(_span('task', None, 'task', '', t))
        return root

    for event in events:
        kind, t = event.get('type'), _number(event.get('t')) or 0.0
        span_id = event.get('span_id') if version == 2 else None
        parent = event.get('parent_id') if version == 2 else None
        if kind == 'task_start':
            if root is None:
                root = add(_span(span_id or 'task', None, 'task', str(event.get('task_id') or ''), t))
                root['attrs']['task_id'] = event.get('task_id')
        elif kind == 'task_end':
            task_end = event
            task = ensure_root(t)
            task['seconds'] = round(max(0.0, t - task['start']), 3)
            task['status'] = str(event.get('status') or 'unknown')
            task['attrs'].update(_copy(event, _TASK_ATTRS))
        elif kind == 'scope_start' and span_id:
            labels = _copy(event, _LABELS)
            scope = event.get('kind') if event.get('kind') in KINDS else 'step'
            label = str(labels.get('member') or labels.get('index') or labels.get('revision') or scope)
            add(_span(span_id, parent, scope, label, t, **labels))
        elif kind == 'scope_end' and span_id in spans:
            scope = spans[span_id]
            scope['seconds'] = _number(event.get('seconds'))
            scope['status'] = 'error' if event.get('error') else 'ok'
            if event.get('error'):
                scope['attrs']['error'] = event['error']
        elif kind in ('before_model', 'before_tool'):
            call = 'model' if kind == 'before_model' else 'tool'
            label = str(event.get('purpose' if call == 'model' else 'tool') or call)
            keys = _MODEL_ATTRS if call == 'model' else _TOOL_ATTRS
            span = add(_span(span_id or f'{call}-{next(numbers)}', parent, call, label, t))
            span['attrs'].update(_copy(event, keys))
            if version == 1:
                pending[call].append(span)
        elif kind in ('after_model', 'after_tool'):
            call = 'model' if kind == 'after_model' else 'tool'
            keys = _MODEL_ATTRS if call == 'model' else _TOOL_ATTRS
            span = spans.get(span_id) if span_id else None
            if span is None and version == 1:
                queue = pending[call]
                name = event.get('purpose' if call == 'model' else 'tool')
                span = next((s for s in queue if s['label'] == str(name)), queue[0] if queue else None)
                if span is not None:
                    queue.remove(span)
            if span is None:            # an after event whose before event was dropped or never written
                label = str(event.get('purpose' if call == 'model' else 'tool') or call)
                seconds = _number(event.get('seconds')) or 0.0
                span = add(_span(span_id or f'{call}-{next(numbers)}', parent, call, label,
                                 round(max(0.0, t - seconds), 3)))
            _finish_call(span, event, keys)
        elif kind == 'todo':
            span = add(_span(f'todo-{next(numbers)}', parent, 'todo', 'todo', t, **_todo_attrs(event)))
            span['seconds'], span['status'] = 0.0, 'ok'
        elif kind == 'step':
            # The progress record of a step attempt, emitted right after its scope ended.
            match = next((s for s in reversed(order) if s['kind'] == 'step'
                          and s['attrs'].get('index') == event.get('step')), None)
            if match is not None:
                match['attrs'].update(_copy(event, _STEP_ATTRS))
                if event.get('outcome') == 'blocked':
                    match['status'] = 'blocked'
                elif event.get('outcome') not in (None, 'succeeded') and match['status'] == 'ok':
                    match['status'] = 'error'
        elif kind == 'plan':
            match = next((s for s in reversed(order) if s['kind'] == 'plan'
                          and s['attrs'].get('revision') == event.get('revision')), None)
            if match is not None and isinstance(event.get('steps'), list):
                match['attrs']['steps'] = len(event['steps'])

    task = ensure_root()
    tops = []
    for span in order:
        if span is task:
            tops.append(span)
        elif version == 1:
            task['children'].append(span)
        elif span['parent'] in spans and span['parent'] != span['id']:
            spans[span['parent']]['children'].append(span)
        elif span['parent'] is None:
            tops.append(span)           # outside the run, e.g. the route choice made before it
        else:
            task['children'].append(span)   # its parent is missing: keep it under the task
    for span in order:
        span['children'].sort(key=lambda s: s['start'])
    tops.sort(key=lambda s: (s['start'], s is task))

    models = [s for s in order if s['kind'] == 'model']

    def total(key):
        return sum(s['attrs'].get(key) or 0 for s in models)
    unfinished = any(s['status'] == 'unfinished' for s in order)
    dropped = header.get('dropped') if type(header.get('dropped')) is int else 0
    trace = {'version': version, 'started_at': header.get('started_at'),
             'task_id': task['attrs'].get('task_id') or (task_end or {}).get('task_id'),
             'status': task['status'] if task_end is not None else 'unfinished', 'seconds': task['seconds'],
             'mode': header.get('mode'), 'read_only': header.get('read_only'), 'dropped': dropped,
             'complete': task_end is not None and not unfinished and not dropped,
             'totals': {'model_calls': len(models), 'tool_calls': sum(1 for s in order if s['kind'] == 'tool'),
                        'prompt_tokens': total('prompt_tokens'), 'completion_tokens': total('completion_tokens'),
                        'cached_tokens': total('cached_tokens')},
             'spans': tops}
    if version == 1:
        trace['paired_by_order'] = True
    return trace


_KIND_TEXT = {'task': '任务', 'route': '路由', 'plan': '规划', 'step': '步骤', 'member': '成员',
              'model': '模型', 'tool': '工具', 'todo': '清单'}
_STATUS_TEXT = {'failed': '失败', 'error': '出错', 'denied': '被拒绝', 'unfinished': '未结束', 'blocked': '等待回答'}


def _seconds(value) -> str:
    if value is None:
        return '—'
    return f'{value:.2f} 秒' if value < 1 else f'{value:.1f} 秒'


def _scalar_args(summary) -> list[str]:
    if not isinstance(summary, dict):
        return []
    return [f'{key}={value}' for key, value in summary.items()
            if isinstance(value, (str, int, float, bool)) and not isinstance(value, dict)]


def _line(span: dict) -> str:
    attrs, kind = span['attrs'], span['kind']
    if kind == 'plan':
        head = f"规划 第 {attrs['revision']} 版" if 'revision' in attrs else '规划'
    elif kind == 'route':
        head = '路由'
    elif kind == 'todo':
        return f"清单 {attrs.get('items', 0)} 项（完成 {attrs.get('done', 0)}）"
    else:
        head = f"{_KIND_TEXT.get(kind, kind)} {span['label']}"
    parts = [head]
    if span['status'] != 'unfinished':
        parts.append(_seconds(span['seconds']))
    if kind == 'model':
        if attrs.get('prompt_tokens') is not None:
            parts.append(f"输入 {attrs['prompt_tokens']:,}")
        if attrs.get('completion_tokens') is not None:
            parts.append(f"输出 {attrs['completion_tokens']:,}")
        if attrs.get('cached_tokens'):
            parts.append(f"缓存 {attrs['cached_tokens']:,}")
        if attrs.get('tool_calls'):
            parts.append(f"请求工具 {attrs['tool_calls']}")
        if attrs.get('provider_calls') and attrs['provider_calls'] > 1:
            parts.append(f"请求 {attrs['provider_calls']} 次")
        if attrs.get('response_model'):
            parts.append(f"服务商模型 {attrs['response_model']}")
    if kind == 'tool' and span['status'] == 'ok':
        parts.append('成功')
    if span['status'] in _STATUS_TEXT:
        parts.append(_STATUS_TEXT[span['status']])
    if attrs.get('denied_by'):
        parts.append(f"由 {attrs['denied_by']}")
    if attrs.get('error'):
        parts.append(f"错误类型 {attrs['error']}")
    if kind == 'tool':
        if span['status'] == 'ok' and attrs.get('chars') is not None:
            parts.append(f"{attrs['chars']:,} 字符")
        parts.extend(_scalar_args(attrs.get('args_summary')))
    if kind == 'step' and attrs.get('runner'):
        parts.append(f"执行方式 {attrs['runner']}")
    return '  '.join(parts)


def _task_line(trace: dict, span: dict) -> str:
    totals = trace['totals']
    task = str(trace.get('task_id') or span['label'] or '')[:8] or '（无编号）'
    return '  '.join([f'任务 {task}', str(trace['status']), _seconds(span['seconds']),
                      f"模型 {totals['model_calls']} 次", f"工具 {totals['tool_calls']} 次",
                      f"输入 {totals['prompt_tokens']:,} / 输出 {totals['completion_tokens']:,} / "
                      f"缓存 {totals['cached_tokens']:,}"])


def render_text(trace: dict) -> str:
    """An indented text tree of one trace, for the command line and for reading by hand."""
    lines = []
    if trace.get('paired_by_order'):
        lines.append('（第 1 版记录：没有区间编号，调用按出现顺序配对，平铺显示）')
    if not trace.get('complete'):
        lines.append('（记录不完整：有调用没有结束记录、事件超过上限被丢弃，或运行没有正常结束）')

    def walk(span, prefix):
        children = span['children']
        for number, child in enumerate(children):
            last = number == len(children) - 1
            lines.append(prefix + ('└─ ' if last else '├─ ') + _line(child))
            walk(child, prefix + ('   ' if last else '│  '))

    for span in trace.get('spans', ()):
        lines.append(_task_line(trace, span) if span['kind'] == 'task' else _line(span))
        walk(span, '')
    return '\n'.join(lines) + '\n'
