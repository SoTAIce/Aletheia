"""Offline tests for read-only batch runner (D-097, docs/batch_run_plan.md)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.__main__ import CliSession
from app.agent import batch
from app.agent.agent_loop import ASK_USER
from app.agent.builtin_hooks import TraceFileHook
from app.agent.hooks import HookSet
from app.agent.run_limits import RunLimits
from app.agent.run_meter import record_model_call
from app.agent.step_models import ModelTurn, ToolDefinition, ToolOutput
from app.agent.tool_calling_model import ToolCallingModelError
from app.config import config
import app.services.rag_agent_service as rag_service_module
from app.services.rag_agent_service import RagAgentService
from app.services.workspace import WorkspacePolicy
from app.tools.tool_registry import ToolRegistry


class ScriptedUsageModel:
    """Offline model returning scripted outcomes and recording every (messages, tools) call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[tuple, tuple]] = []

    async def generate(self, messages, tools=()):
        self.calls.append((tuple(messages), tuple(tools)))
        action = self._responses.pop(0)
        if callable(action):
            action = action(messages, tools)
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, tuple):
            turn, prompt_tokens, completion_tokens = action
        else:
            turn, prompt_tokens, completion_tokens = action, 10, 5
        record_model_call(SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))
        return turn


def _make_registry(*, with_writes: bool = True) -> ToolRegistry:
    registry = ToolRegistry()

    def add(name: str, effect: str):
        async def invoke(args):
            return ToolOutput(True, f'ok:{name}')
        registry.register(SimpleNamespace(
            definition=ToolDefinition(name, name, {'type': 'object'}, effect=effect),
            invoke=invoke,
        ))

    add('read_file', 'read')
    if with_writes:
        add('write_file', 'write')
        add('run_tests', 'run')
    return registry


@pytest.fixture(autouse=True)
def _fake_registry(monkeypatch):
    @asynccontextmanager
    async def fake_open_tool_registry(*args, **kwargs):
        yield _make_registry(with_writes=True)

    monkeypatch.setattr(rag_service_module, 'open_tool_registry', fake_open_tool_registry)


def _opener_for(model, *, hooks=None):
    async def opener(svc_args, stack):
        svc = await stack.enter_async_context(RagAgentService(
            model=model,
            mcp_servers=(),
            include_knowledge=False,
            memory_directory='',
            personas_dir='',
            hooks=hooks,
            max_executions=svc_args.max_executions,
        ))
        return CliSession(svc, None, None, None, (), svc_args.filesystem_root, None)
    return opener


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


# ---- Manifest parsing ----

@pytest.mark.parametrize('content, expected_line', [
    ('not json\n', '第 1 行'),
    ('["list"]\n', '第 1 行'),
    ('{"goal": "hi"}\n', '第 1 行'),
    ('{"id": "   ", "goal": "hi"}\n', '第 1 行'),
    ('{"id": "' + 'a' * 101 + '", "goal": "hi"}\n', '第 1 行'),
    ('{"id": "a", "goal": ""}\n', '第 1 行'),
    ('{"id": "a", "goal": "one"}\n{"id": "a", "goal": "two"}\n', '第 2 行'),
])
def test_manifest_errors_report_chinese_message_with_line_number(tmp_path, content, expected_line):
    manifest = tmp_path / 'bad.jsonl'
    manifest.write_text(content, encoding='utf-8')
    with pytest.raises(batch.BatchError) as exc_info:
        batch.load_manifest(manifest)
    assert expected_line in str(exc_info.value)


def test_manifest_empty_is_rejected(tmp_path):
    manifest = tmp_path / 'empty.jsonl'
    manifest.write_text('\n  \n', encoding='utf-8')
    with pytest.raises(batch.BatchError, match='为空'):
        batch.load_manifest(manifest)


# ---- --each-file expansion & CLI argument validation ----

def test_each_file_expands_sorted_files_skips_closed_folders_and_outside_links(tmp_path):
    root = tmp_path / 'work'
    (root / 'docs').mkdir(parents=True)
    (root / 'docs' / 'b.md').write_text('b', encoding='utf-8')
    (root / 'docs' / 'a.md').write_text('a', encoding='utf-8')
    (root / 'docs' / 'sub').mkdir()  # directory, must be skipped
    secret_dir = root / 'closed_secret'
    secret_dir.mkdir()
    (secret_dir / 'hidden.md').write_text('secret', encoding='utf-8')

    policy = WorkspacePolicy(deny=(secret_dir.resolve(),), protected=(), allowed_roots=())
    items = batch.expand_each_file('**/*.md', '总结 {file}', root, policy=policy)
    assert [i.id for i in items] == ['docs/a.md', 'docs/b.md']
    assert [i.goal for i in items] == ['总结 docs/a.md', '总结 docs/b.md']


def test_each_file_rejects_over_500_items_and_missing_placeholder(tmp_path):
    root = tmp_path / 'many'
    root.mkdir()
    for i in range(501):
        (root / f'f{i:03d}.txt').write_text('x', encoding='utf-8')
    policy = WorkspacePolicy(deny=(), protected=(), allowed_roots=())
    with pytest.raises(batch.BatchError, match='500'):
        batch.expand_each_file('*.txt', '处理 {file}', root, policy=policy)
    with pytest.raises(batch.BatchError, match=r'\{file\}'):
        batch.expand_each_file('*.txt', '没有占位符', root, policy=policy)


def test_parse_args_enforces_mutual_exclusion_required_max_tokens_and_readonly():
    # manifest and --each-file cannot be combined
    with pytest.raises(SystemExit):
        batch.parse_args(['m.jsonl', '--each-file', '*.md', '--goal', '{file}', '--out', 'o.jsonl', '--max-tokens', '100'])
    # missing --max-tokens
    with pytest.raises(SystemExit):
        batch.parse_args(['m.jsonl', '--out', 'o.jsonl'])
    # rejects write/sandbox/execution-mode flags
    for bad_flag in ('--write-root', '--sandbox', '--execution-mode', '--interactive', '--result-json'):
        with pytest.raises(SystemExit):
            batch.parse_args(['m.jsonl', '--out', 'o.jsonl', '--max-tokens', '100', bad_flag])


# ---- Confirmation ----

def test_confirmation_blocks_non_tty_and_cancel_without_creating_out(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text('{"id": "1", "goal": "hi"}\n', encoding='utf-8')
    out = tmp_path / 'out.jsonl'
    args = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '1000'])

    lines: list[str] = []
    code = asyncio.run(batch.run_batch(args, write=lines.append, interactive=False,
                                       service_opener=_opener_for(ScriptedUsageModel([]))))
    assert code == 2
    assert not out.exists()
    assert any('--confirm-batch' in line for line in lines)

    lines.clear()
    code = asyncio.run(batch.run_batch(args, write=lines.append, ask=lambda _: 'n', interactive=True,
                                       service_opener=_opener_for(ScriptedUsageModel([]))))
    assert code == 2
    assert not out.exists()


# ---- Sequential execution, isolation, read-only, no ask_user ----

def test_batch_runs_items_sequentially_isolated_readonly_and_without_ask_user(tmp_path, monkeypatch):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text(
        '{"id": "i1", "goal": "First secret alpha"}\n'
        '{"id": "i2", "goal": "Second fails"}\n'
        '{"id": "i3", "goal": "Third task"}\n',
        encoding='utf-8',
    )
    out = tmp_path / 'out.jsonl'
    monkeypatch.setattr(config, 'mcp_filesystem_write_root', str(tmp_path))

    model = ScriptedUsageModel([
        (ModelTurn('ANSWER_ONE_SECRET'), 100, 20),
        ToolCallingModelError('boom'),
        (ModelTurn('ANSWER_THREE'), 50, 10),
    ])
    args = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '10000', '--confirm-batch'])
    printed: list[str] = []
    code = asyncio.run(batch.run_batch(args, write=printed.append, service_opener=_opener_for(model)))

    assert code == 1
    assert any('批处理只读，不会使用写入目录' in line for line in printed)
    # Answers must never be printed to terminal
    assert not any('ANSWER_ONE_SECRET' in line or 'ANSWER_THREE' in line for line in printed)

    rows = _read_jsonl(out)
    assert rows[0]['type'] == 'batch' and rows[0]['items'] == 3
    items_by_id = {r['id']: r for r in rows[1:]}
    assert items_by_id['i1']['status'] == 'completed'
    assert items_by_id['i1']['answer'] == 'ANSWER_ONE_SECRET'
    assert items_by_id['i1']['tokens'] == 120
    assert items_by_id['i2']['status'] == 'failed'
    assert items_by_id['i3']['status'] == 'completed'
    assert items_by_id['i3']['answer'] == 'ANSWER_THREE'

    # Each item had isolated session: item 2 and 3 prompts must not contain item 1's text
    assert len(model.calls) == 3
    for idx in (1, 2):
        prompt_text = '\n'.join(getattr(m, 'content', '') for m in model.calls[idx][0])
        assert 'First secret alpha' not in prompt_text
        assert 'ANSWER_ONE_SECRET' not in prompt_text

    # Write tools and ask_user must be hidden from every item
    for _, tools in model.calls:
        tool_names = {t.name for t in tools}
        assert 'write_file' not in tool_names
        assert 'run_tests' not in tool_names
        assert ASK_USER not in tool_names
        assert 'read_file' in tool_names


# ---- Budgets ----

def test_item_budget_exceeded_fails_only_that_item(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text(
        '{"id": "a", "goal": "needs two calls"}\n'
        '{"id": "b", "goal": "fits easily"}\n',
        encoding='utf-8',
    )
    out = tmp_path / 'out.jsonl'
    from app.agent.step_models import ToolCall
    model = ScriptedUsageModel([
        # Item a call 1 uses 100 tokens (>= item_max_tokens=80) and asks for a tool -> next call hits budget
        (ModelTurn('', (ToolCall('c1', 'read_file', {}),)), 60, 40),
        # Item b succeeds within its own 80-token budget
        (ModelTurn('done b'), 20, 10),
    ])
    args = batch.parse_args([
        str(manifest), '--out', str(out),
        '--max-tokens', '1000', '--item-max-tokens', '80', '--confirm-batch',
    ])
    code = asyncio.run(batch.run_batch(args, write=lambda _: None, service_opener=_opener_for(model)))
    assert code == 1
    rows = _read_jsonl(out)[1:]
    assert rows[0]['id'] == 'a' and rows[0]['status'] == 'failed' and rows[0]['failure_kind'] == 'budget'
    assert rows[1]['id'] == 'b' and rows[1]['status'] == 'completed' and rows[1]['answer'] == 'done b'


def test_batch_budget_exhausted_marks_remaining_not_run_without_model_calls(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text(
        '{"id": "a", "goal": "one"}\n'
        '{"id": "b", "goal": "two"}\n'
        '{"id": "c", "goal": "three"}\n',
        encoding='utf-8',
    )
    out = tmp_path / 'out.jsonl'
    model = ScriptedUsageModel([
        (ModelTurn('done a'), 80, 30),  # 110 tokens >= max_tokens=100
    ])
    args = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '100', '--confirm-batch'])
    code = asyncio.run(batch.run_batch(args, write=lambda _: None, service_opener=_opener_for(model)))
    assert code == 2
    assert len(model.calls) == 1  # b and c never called the model
    rows = _read_jsonl(out)[1:]
    # Plan 4.2: the item that exhausts the batch budget fails normally; remaining items are not_run.
    assert [r['status'] for r in rows] == ['failed', 'not_run', 'not_run']
    assert rows[0]['failure_kind'] == 'budget'


# ---- Resume ----

def test_resume_skips_completed_reruns_failed_handles_truncated_tail_and_last_line_wins(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text(
        '{"id": "a", "goal": "one"}\n'
        '{"id": "b", "goal": "two"}\n'
        '{"id": "c", "goal": "three"}\n',
        encoding='utf-8',
    )
    out = tmp_path / 'out.jsonl'

    # First run: a completes, b fails, c completes
    model1 = ScriptedUsageModel([
        (ModelTurn('A1'), 10, 5),
        ToolCallingModelError('transient'),
        (ModelTurn('C1'), 10, 5),
    ])
    args = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '1000', '--confirm-batch'])
    assert asyncio.run(batch.run_batch(args, write=lambda _: None, service_opener=_opener_for(model1))) == 1

    # Simulate a truncated trailing line after c
    with out.open('ab') as fp:
        fp.write(b'{"type": "item", "id": "b", "status": "com')

    # Second run: ignores truncated tail, skips a & c, re-runs only b
    model2 = ScriptedUsageModel([(ModelTurn('B2'), 12, 8)])
    notices: list[str] = []
    code = asyncio.run(batch.run_batch(args, write=notices.append, service_opener=_opener_for(model2)))
    assert code == 0
    assert len(model2.calls) == 1
    assert any('不是完整 JSON' in n for n in notices)

    rows = _read_jsonl(out)
    b_rows = [r for r in rows[1:] if r['id'] == 'b']
    assert len(b_rows) == 2
    assert b_rows[0]['status'] == 'failed'
    assert b_rows[-1]['status'] == 'completed' and b_rows[-1]['answer'] == 'B2'


def test_resume_rejects_mismatched_manifest_without_touching_out(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text('{"id": "a", "goal": "one"}\n', encoding='utf-8')
    out = tmp_path / 'out.jsonl'
    args = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '1000', '--confirm-batch'])
    assert asyncio.run(batch.run_batch(args, write=lambda _: None,
                                       service_opener=_opener_for(ScriptedUsageModel([ModelTurn('ok')])))) == 0
    before = out.read_bytes()

    # Change manifest goal -> batch_id changes
    manifest.write_text('{"id": "a", "goal": "changed"}\n', encoding='utf-8')
    messages: list[str] = []
    code = asyncio.run(batch.run_batch(args, write=messages.append,
                                       service_opener=_opener_for(ScriptedUsageModel([]))))
    assert code == 2
    assert out.read_bytes() == before
    assert any('批次编号' in m for m in messages)


def test_resume_with_skip_failed_skips_failed_items(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text('{"id": "a", "goal": "one"}\n', encoding='utf-8')
    out = tmp_path / 'out.jsonl'
    args1 = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '1000', '--confirm-batch'])
    assert asyncio.run(batch.run_batch(args1, write=lambda _: None,
                                       service_opener=_opener_for(ScriptedUsageModel([ToolCallingModelError('e')])))) == 1

    model2 = ScriptedUsageModel([])
    args2 = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '1000',
                              '--skip-failed', '--confirm-batch'])
    code = asyncio.run(batch.run_batch(args2, write=lambda _: None, service_opener=_opener_for(model2)))
    assert code == 1
    assert len(model2.calls) == 0


# ---- Output contract ----

def test_batch_output_contract_populates_output_and_marks_invalid_as_contract_failure(tmp_path):
    schema_path = tmp_path / 'schema.json'
    schema_path.write_text(json.dumps({
        'type': 'object',
        'properties': {'score': {'type': 'integer'}},
        'required': ['score'],
        'additionalProperties': False,
    }), encoding='utf-8')
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text(
        '{"id": "ok", "goal": "give score"}\n'
        '{"id": "bad", "goal": "give score"}\n',
        encoding='utf-8',
    )
    out = tmp_path / 'out.jsonl'
    model = ScriptedUsageModel([
        ModelTurn('{"score": 9}'),
        ModelTurn('not json'),
        ModelTurn('still not json'),
    ])
    args = batch.parse_args([
        str(manifest), '--out', str(out), '--max-tokens', '1000',
        '--output-schema', str(schema_path), '--confirm-batch',
    ])
    code = asyncio.run(batch.run_batch(args, write=lambda _: None, service_opener=_opener_for(model)))
    assert code == 1
    rows = {r['id']: r for r in _read_jsonl(out)[1:]}
    assert rows['ok']['status'] == 'completed' and rows['ok']['output'] == {'score': 9}
    assert rows['bad']['status'] == 'failed' and rows['bad']['failure_kind'] == 'contract'


# ---- Privacy (D-059): answers never enter trace or hook events ----

def test_batch_answers_do_not_enter_trace_or_hook_events(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text('{"id": "s1", "goal": "tell secret"}\n', encoding='utf-8')
    out = tmp_path / 'out.jsonl'
    trace_dir = tmp_path / 'traces'
    seen_events: list[dict] = []

    class CaptureHook:
        def observe(self, event):
            seen_events.append(dict(event))

    hooks = HookSet([TraceFileHook(trace_dir), CaptureHook()])
    secret = 'TOP_SECRET_BATCH_ANSWER_987654'
    model = ScriptedUsageModel([ModelTurn(secret)])
    args = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '1000', '--confirm-batch'])
    printed: list[str] = []
    code = asyncio.run(batch.run_batch(args, write=printed.append,
                                       service_opener=_opener_for(model, hooks=hooks)))
    assert code == 0
    assert secret in out.read_text(encoding='utf-8')
    assert secret not in '\n'.join(printed)
    assert secret not in json.dumps(seen_events, ensure_ascii=False)
    for trace_file in trace_dir.rglob('*.jsonl'):
        assert secret not in trace_file.read_text(encoding='utf-8')


# ---- Ctrl+C / cancellation ----

def test_batch_records_cancelled_item_and_returns_130_on_interrupt(tmp_path):
    manifest = tmp_path / 'm.jsonl'
    manifest.write_text('{"id": "i1", "goal": "one"}\n{"id": "i2", "goal": "two"}\n', encoding='utf-8')
    out = tmp_path / 'out.jsonl'
    model = ScriptedUsageModel([asyncio.CancelledError()])
    args = batch.parse_args([str(manifest), '--out', str(out), '--max-tokens', '1000', '--confirm-batch'])
    code = asyncio.run(batch.run_batch(args, write=lambda _: None, service_opener=_opener_for(model)))
    assert code == 130
    rows = _read_jsonl(out)[1:]
    assert len(rows) == 1 and rows[0]['id'] == 'i1' and rows[0]['status'] == 'cancelled'


# ---- Service allow_ask_user contract & RunLimits ----

async def test_service_allow_ask_user_false_hides_ask_user_and_rejects_resume():
    model = ScriptedUsageModel([ModelTurn('done')])
    events: list[dict] = []
    async with RagAgentService(model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
                               personas_dir='') as service:
        res = await service.query_result('hi', 's1', allow_ask_user=False, on_event=events.append)
        assert res.content == 'done'
        tool_names = {t.name for t in model.calls[0][1]}
        assert ASK_USER not in tool_names
        route_event = next(e for e in events if e.get('type') == 'route')
        assert route_event['options']['ask_user'] is False

        # Default allow_ask_user=True leaves options unchanged (no ask_user key)
        model2_events: list[dict] = []
        model._responses.append(ModelTurn('ok2'))
        await service.query_result('hi2', 's2', on_event=model2_events.append)
        default_route = next(e for e in model2_events if e.get('type') == 'route')
        assert 'ask_user' not in default_route['options']
        assert ASK_USER in {t.name for t in model.calls[1][1]}

        # allow_ask_user=False on resume is refused
        with pytest.raises(ValueError, match='allow_ask_user'):
            await service.query_result('ans', 's1', resume_task_id='t1', allow_ask_user=False)


def test_run_limits_includes_batch_defaults():
    rows = {name: value for layer, name, value in RunLimits.current().rows() if layer == '批处理'}
    assert rows['每项默认令牌预算（--item-max-tokens）'] == '200,000'
    assert rows['每项默认时间预算（--item-max-seconds）'] == '300 秒'
