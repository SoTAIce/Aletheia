"""Rule-based tool permission control (D-094, docs/permission_rules_plan.md)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

from app.agent.__main__ import main as cli_main, parse_args
from app.agent.hooks import (
    AFTER_TOOL, BEFORE_TOOL, HookSet, HookedTool, extend_deadlines, pausable_deadline, using_hooks,
)
from app.agent.permission_rules import (
    MAX_RULES, PermissionRules, PermissionRulesError, RuleGate, load_permission_rules, parse_permission_rules,
)
from app.agent.run_meter import RunMeter, metering
from app.agent.step_models import ModelTurn, ToolCall, ToolDefinition, ToolOutput
from app.agent.terminal_input import STALE_INPUT_NOTICE, TerminalAsker, TerminalReader, format_approval_lines
from app.agent.trace_view import build_trace, render_text
from app.config import Settings
from app.services.rag_agent_service import RagAgentService
from app.tools.filesystem.file_access import FileAccess
from app.tools.tool_loader import workspace_servers
from loop_fakes import loop_reply


class _FakeTool:
    def __init__(self, name: str, effect: str | None = None) -> None:
        self.definition = ToolDefinition(name=name, description='fake', input_schema={}, effect=effect)
        self.calls: list[dict] = []

    async def invoke(self, arguments: dict) -> ToolOutput:
        self.calls.append(dict(arguments))
        return ToolOutput(True, f'ok:{self.definition.name}')


class _Recorder:
    name = 'recorder'

    def __init__(self) -> None:
        self.events: list[dict] = []

    def observe(self, event: dict) -> None:
        self.events.append(event)


def _rules_from_toml(tmp_path: Path, text: str) -> PermissionRules:
    path = tmp_path / 'permissions.toml'
    path.write_text(text, encoding='utf-8')
    return load_permission_rules(path)


# --- Parsing ---

def test_parse_valid_rules_and_example_file():
    example = Path(__file__).resolve().parents[1] / 'permissions.example.toml'
    rules = load_permission_rules(example)
    assert len(rules) == 4
    assert [(r.index, r.action, r.for_tool_server) for r in rules] == [
        (1, 'allow', False),
        (2, 'ask', False),
        (3, 'deny', True),
        (4, 'allow', False),
    ]
    assert rules.rules[1].tools == ('write_file', 'edit_file')
    assert rules.rules[1].path_parts == ('docs',)
    assert rules.rules[3].max_calls == 5


@pytest.mark.parametrize('toml_text', [
    'other = 1\n',
    'rule = "not-a-list"\n',
    '[[rule]]\ntool = "read_file"\n',                                 # missing action
    '[[rule]]\naction = "block"\n',                                   # bad action
    '[[rule]]\naction = "allow"\nextra = 1\n',                        # unknown field
    '[[rule]]\naction = "allow"\ntool = ""\n',                        # empty tool
    '[[rule]]\naction = "allow"\ntool = []\n',                        # empty tool list
    '[[rule]]\naction = "allow"\neffect = "network"\n',               # unknown effect
    '[[rule]]\naction = "deny"\npath = "/etc/passwd"\n',              # POSIX absolute path
    '[[rule]]\naction = "deny"\npath = "C:/Windows"\n',               # Windows drive path
    '[[rule]]\naction = "deny"\npath = "\\\\server\\share"\n',        # UNC path
    '[[rule]]\naction = "deny"\npath = "../secrets"\n',               # parent traversal
    '[[rule]]\naction = "deny"\npath = "docs/../secrets"\n',          # embedded ..
    '[[rule]]\naction = "deny"\npath = "."\n',                        # dot
    '[[rule]]\naction = "deny"\npath = ""\n',                         # empty path
    '[[rule]]\naction = "deny"\npath = "secrets/"\nmax_calls = 1\n',  # max_calls with deny
    '[[rule]]\naction = "allow"\nmax_calls = 0\n',                    # non-positive max_calls
    '[[rule]]\naction = "allow"\nmax_calls = true\n',                 # bool max_calls
    '[[rule]]\naction = [invalid toml\n',                             # invalid TOML
])
def test_parse_rejects_invalid_rules(tmp_path, toml_text):
    path = tmp_path / 'bad.toml'
    path.write_text(toml_text, encoding='utf-8')
    with pytest.raises(PermissionRulesError):
        load_permission_rules(path)


def test_parse_rejects_more_than_200_rules():
    data = {'rule': [{'action': 'allow', 'tool': 'read_file'} for _ in range(MAX_RULES + 1)]}
    with pytest.raises(PermissionRulesError, match='200'):
        parse_permission_rules(data)


def test_settings_default_absent_vs_explicit_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('PERMISSIONS_FILE', raising=False)
    assert Settings(_env_file=None).permission_rules() is None
    assert Settings(_env_file=None, permissions_file='').permission_rules() is None
    with pytest.raises(PermissionRulesError, match='PERMISSIONS_FILE'):
        Settings(_env_file=None, permissions_file=str(tmp_path / 'missing.toml')).permission_rules()


# --- Matching ---

async def test_matching_tools_effects_and_path_segments(tmp_path):
    rules = _rules_from_toml(tmp_path, '''
[[rule]]
tool = ["write_file", "edit_file"]
path = "docs/"
action = "deny"

[[rule]]
effect = "external"
action = "deny"
''')
    gate = RuleGate(rules, workspace=r'D:\proj', case_insensitive=True)
    # Segment prefix: docs matches docs/a.md, not docsx/a.md.
    d1 = await gate.check({'type': BEFORE_TOOL, 'tool': 'write_file', 'effect': 'write',
                           'args_preview': {'path': r'.\docs\..\DOCS\a.md'}})
    assert not d1.allow and d1.rule == 1
    d2 = await gate.check({'type': BEFORE_TOOL, 'tool': 'write_file', 'effect': 'write',
                           'args_preview': {'path': 'docsx/a.md'}})
    assert d2.allow
    # Absolute path inside workspace is normalized to relative.
    d3 = await gate.check({'type': BEFORE_TOOL, 'tool': 'edit_file', 'effect': 'write',
                           'args_preview': {'path': r'd:\proj\docs\sub\guide.md'}})
    assert not d3.allow and d3.rule == 1
    # Preview dict shape (used by read_file/list_directory/search_files).
    rules_read = _rules_from_toml(tmp_path, '[[rule]]\npath = "secrets/"\naction = "deny"\n')
    gate_read = RuleGate(rules_read, workspace=r'D:\proj', case_insensitive=True)
    d4 = await gate_read.check({'type': BEFORE_TOOL, 'tool': 'read_file', 'effect': 'read',
                                'args_preview': {'path': {'text': 'secrets/key.txt', 'chars': 15, 'omitted_chars': 0}}})
    assert not d4.allow and d4.rule == 1
    # Call without path does not match path rules.
    d5 = await gate_read.check({'type': BEFORE_TOOL, 'tool': 'search_web', 'effect': 'read',
                                'args_preview': {'query': {'text': 'secrets', 'chars': 7, 'omitted_chars': 0}}})
    assert d5.allow
    # Effect matching.
    d6 = await gate.check({'type': BEFORE_TOOL, 'tool': 'third_party_tool', 'effect': 'external',
                           'args_preview': {}})
    assert not d6.allow and d6.rule == 2


async def test_unknown_paths_match_deny_and_ask_but_not_allow(tmp_path):
    rules = _rules_from_toml(tmp_path, '''
[[rule]]
path = "docs/"
action = "allow"

[[rule]]
path = "secrets/"
action = "deny"
''')
    gate = RuleGate(rules, workspace=r'D:\proj', case_insensitive=True)
    for bad_path in (
        r'D:\other\file.txt',                                               # outside workspace
        '../escape.txt',                                                    # escapes workspace
        {'text': 'docs/a.md', 'chars': 3000, 'omitted_chars': 1000},        # truncated
        {'type': 'int'},                                                    # non-string
    ):
        decision = await gate.check({'type': BEFORE_TOOL, 'tool': 'read_file', 'effect': 'read',
                                     'args_preview': {'path': bad_path}})
        assert not decision.allow and decision.rule == 2

    ask_rules = _rules_from_toml(tmp_path, '[[rule]]\npath = "docs/"\naction = "ask"\n')
    ask_gate = RuleGate(ask_rules, workspace=r'D:\proj', asker=None)
    decision = await ask_gate.check({'type': BEFORE_TOOL, 'tool': 'write_file', 'effect': 'write',
                                     'args_preview': {'path': '../outside.md'}})
    assert not decision.allow and decision.rule == 1 and 'no one can answer' in decision.reason


# --- Decision priority, defaults, max_calls ---

async def test_decision_priority_and_max_calls(tmp_path):
    rules = _rules_from_toml(tmp_path, '''
[[rule]]
tool = "write_file"
action = "allow"

[[rule]]
tool = "write_file"
path = "docs/"
action = "ask"

[[rule]]
path = "docs/locked/"
action = "deny"
''')
    asked_calls = []

    class _SimpleAsker:
        wait_seconds = 10

        def __init__(self, allow_seq):
            self._seq = iter(allow_seq)

        async def ask(self, event, *, rule=None):
            from app.agent.hooks import ALLOW, Decision
            asked_calls.append(rule)
            return ALLOW if next(self._seq) else Decision.deny(f'the user did not allow this call (permission rule {rule})', rule=rule)

    gate = RuleGate(rules, workspace=r'D:\proj', asker=_SimpleAsker([True]))
    # Deny beats ask and allow.
    d_deny = await gate.check({'type': BEFORE_TOOL, 'tool': 'write_file', 'effect': 'write',
                               'args_preview': {'path': 'docs/locked/a.md'}})
    assert not d_deny.allow and d_deny.rule == 3 and d_deny.reason == 'denied by permission rule 3'
    # Ask beats allow.
    d_ask = await gate.check({'type': BEFORE_TOOL, 'tool': 'write_file', 'effect': 'write',
                              'args_preview': {'path': 'docs/ok.md'}})
    assert d_ask.allow and asked_calls == [2]

    # max_calls: denied calls do not count; allowed calls count up to the limit.
    cap_rules = _rules_from_toml(tmp_path, '[[rule]]\ntool = "search_web"\naction = "ask"\nmax_calls = 2\n')
    cap_gate = RuleGate(cap_rules, asker=_SimpleAsker([False, True, True]))
    e = {'type': BEFORE_TOOL, 'tool': 'search_web', 'effect': 'read', 'args_preview': {}}
    assert not (await cap_gate.check(e)).allow     # denied by user -> does not count
    assert (await cap_gate.check(e)).allow         # count = 1
    assert (await cap_gate.check(e)).allow         # count = 2
    capped = await cap_gate.check(e)
    assert not capped.allow and capped.rule == 1
    assert capped.reason == 'denied by permission rule 1: this task already made 2 calls (limit 2)'


# --- TerminalReader and TerminalAsker ---

async def test_terminal_asker_y_a_deny_eof_and_formatting():
    outputs: list[str] = []
    answers = iter(['y', 'n', 'a'])
    reader = TerminalReader(read_fn=lambda prompt: next(answers), write=outputs.append)
    asker = TerminalAsker(reader, wait_seconds=5, write=outputs.append)
    event = {
        'type': BEFORE_TOOL,
        'tool': 'write_file',
        'args_preview': {
            'path': 'docs/readme.md',
            'content': {'text': '\n'.join(f'line {i} ' + 'x' * 220 for i in range(1, 8)), 'chars': 1600, 'omitted_chars': 0},
        },
    }
    lines = format_approval_lines(event, rule=2)
    assert any('权限规则 2' in l and 'write_file' in l for l in lines)
    assert any('docs/readme.md' in l for l in lines)
    assert any('1600' in l for l in lines)
    assert any('其余内容已省略' in l for l in lines)
    assert all(len(l) <= 200 for l in lines)

    edit_lines = format_approval_lines({
        'tool': 'edit_file',
        'args_preview': {'path': 'a.py', 'old': {'chars': 10}, 'new': {'chars': 25}},
    }, rule=1)
    assert any('10' in l and '25' in l for l in edit_lines)

    py_lines = format_approval_lines({
        'tool': 'run_python',
        'args_preview': {'path': 'run.py', 'args': {'items': ['--flag', 'val'], 'omitted_items': 0, 'omitted_chars': 0}},
    }, rule=1)
    assert any('--flag val' in l for l in py_lines)

    assert (await asker.ask(event, rule=2)).allow
    denied = await asker.ask(event, rule=2)
    assert not denied.allow and denied.rule == 2 and '(permission rule 2)' in denied.reason
    assert (await asker.ask(event, rule=2)).allow  # 'a' -> allow_all
    assert (await asker.ask(event, rule=2)).allow  # skips prompt

    # EOFError is treated as denial.
    def _eof(prompt):
        raise EOFError
    eof_asker = TerminalAsker(TerminalReader(read_fn=_eof, write=outputs.append), wait_seconds=5, write=outputs.append)
    eof_dec = await eof_asker.ask(event, rule=3)
    assert not eof_dec.allow and eof_dec.rule == 3


async def test_terminal_reader_discards_late_answer_after_timeout():
    release = threading.Event()
    calls = 0
    notices: list[str] = []

    def fake_input(prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            release.wait(timeout=5)
            return 'y'          # late answer to the timed-out confirmation
        return 'next goal'

    reader = TerminalReader(read_fn=fake_input, write=notices.append)
    asker = TerminalAsker(reader, wait_seconds=0.05, write=notices.append)
    meter = RunMeter(timeout_seconds=1.0)
    with metering(meter):
        async with asyncio.timeout(0.5) as deadline:
            with pausable_deadline(deadline):
                decision = await asker.ask({'tool': 'write_file', 'args_preview': {'path': 'a.txt'}}, rule=1)
    assert not decision.allow and decision.rule == 1 and 'did not answer' in decision.reason
    assert any('按拒绝处理' in n and '第一行会被忽略' in n for n in notices)   # said at once, not later

    # Unblock the stale input thread while the interactive loop asks for the next goal.
    release.set()
    line = await reader.read_line('> ')
    assert line == 'next goal'
    assert STALE_INPUT_NOTICE in notices


# --- Tool-server bottom line ("交给工具") ---

def test_path_only_deny_reaches_tool_server_and_blocks_search_files(tmp_path):
    rules = _rules_from_toml(tmp_path, '''
[[rule]]
path = "secrets/"
action = "deny"

[[rule]]
tool = "write_file"
path = "docs/"
action = "deny"
''')
    workspace = tmp_path / 'ws'
    (workspace / 'secrets').mkdir(parents=True)
    (workspace / 'secrets' / 'token.txt').write_text('TOP_SECRET_NEEDLE', encoding='utf-8')
    (workspace / 'public.txt').write_text('hello world', encoding='utf-8')

    # workspace_servers forwards only path-only deny rules as --deny.
    factory = workspace_servers(permission_rules=rules)
    servers = factory(workspace, False)
    assert len(servers) == 1
    args = servers[0].args
    assert '--deny' in args
    denied_arg = args[args.index('--deny') + 1]
    assert Path(denied_arg) == (workspace / 'secrets').resolve()
    assert not any('docs' in a for a in args)

    # Real FileAccess hides secrets/ from search_files at the root and refuses direct reads.
    access = FileAccess(workspace, denied=rules.deny_paths_for(workspace))
    results = access.search_files('TOP_SECRET_NEEDLE', path='.')
    assert results['matches'] == []
    with pytest.raises(PermissionError):
        access.read_file('secrets/token.txt')


# --- Recording, usage, trace_view ---

async def test_events_usage_and_trace_record_rule_without_leaking_secret_path(tmp_path):
    rules = _rules_from_toml(tmp_path, '[[rule]]\npath = "secrets/"\naction = "deny"\n')
    secret_path = 'secrets/' + 's' * 250 + '/vault.key'
    tool = HookedTool(_FakeTool('write_file', effect='write'))
    recorder = _Recorder()
    gate = RuleGate(rules, workspace=tmp_path)
    meter = RunMeter()
    with metering(meter), using_hooks(HookSet([recorder], [gate])):
        output = await tool.invoke({'path': secret_path, 'content': 'top-secret'})
    assert not output.success
    assert output.error == 'not executed: denied by permission rule 1'
    assert secret_path not in output.error
    after = next(e for e in recorder.events if e['type'] == AFTER_TOOL)
    assert after['denied_by'] == 'permissions' and after['rule'] == 1
    assert secret_path not in json.dumps(recorder.events, ensure_ascii=False)
    assert meter.snapshot()['permissions'] == {'denied': 1, 'asked': 0}

    # Trace rendering shows "被权限规则 1 拒绝".
    trace = build_trace(recorder.events)
    rendered = render_text(trace)
    assert '被权限规则 1 拒绝' in rendered


async def test_a_request_gate_learns_the_task_directory_from_the_service(tmp_path):
    # The web run cannot know a resumed task's checked directory; the service tells its gate.
    rules = _rules_from_toml(tmp_path, '[[rule]]\npath = "secrets/"\naction = "deny"\n')
    workspace = tmp_path / 'ws'
    request_gate = RuleGate(rules)
    async with RagAgentService(model=AsyncMock(), mcp_servers=(), include_knowledge=False,
                               memory_directory=tmp_path, permission_rules=rules) as svc:
        hooks, _ = await svc._run_hooks('k', SimpleNamespace(archive=None), HookSet(gates=[request_gate]),
                                        workspace=str(workspace))
    assert [g for g in hooks.gates if isinstance(g, RuleGate)] == [request_gate]   # not doubled
    event = {'type': 'before_tool', 'tool': 'read_file', 'effect': 'read',
             'args_preview': {'path': {'text': str(workspace / 'secrets' / 'a.txt'), 'chars': 1,
                                       'omitted_chars': 0}}}
    decision = await request_gate.check(event)
    assert not decision.allow and decision.rule == 1
    other = dict(event, args_preview={'path': {'text': str(workspace / 'public.txt'), 'chars': 1,
                                               'omitted_chars': 0}})
    assert (await request_gate.check(other)).allow     # known directory: not "unknown", so allowed


async def test_service_without_rules_leaves_usage_unchanged(tmp_path):
    model = AsyncMock()
    model.generate.side_effect = [loop_reply('done')]
    async with RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                               memory_directory=tmp_path) as svc:
        result = await svc.query_result('hi', 's1')
    assert 'permissions' not in result.usage


# --- CLI --show-permissions ---

async def test_cli_show_permissions(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('PERMISSIONS_FILE', raising=False)
    monkeypatch.setattr('app.agent.__main__.config', Settings(_env_file=None))
    assert await cli_main(parse_args(['--show-permissions'])) == 0
    assert '没有规则文件（PERMISSIONS_FILE），行为和以前一样' in capsys.readouterr().out

    rules_file = tmp_path / 'my_rules.toml'
    rules_file.write_text('[[rule]]\npath = "secrets/"\naction = "deny"\n', encoding='utf-8')
    monkeypatch.setattr('app.agent.__main__.config', Settings(_env_file=None, permissions_file=str(rules_file)))
    assert await cli_main(parse_args(['--show-permissions'])) == 0
    out = capsys.readouterr().out
    assert 'secrets/' in out and '交给工具' in out and '是' in out

    monkeypatch.setattr('app.agent.__main__.config',
                        Settings(_env_file=None, permissions_file=str(tmp_path / 'nope.toml')))
    assert await cli_main(parse_args(['--show-permissions'])) == 2
    assert 'PermissionRulesError:' in capsys.readouterr().out
