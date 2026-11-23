"""User-configured stdio MCP servers (D-095, docs/user_mcp_plan.md)."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.types import CallToolResult, ImageContent, TextContent, Tool as McpTool, ToolAnnotations
import pytest

from app.agent.__main__ import _notice_to, main as cli_main, parse_args, show_limits, show_mcp, team_options_for
from app.agent.delegation import DelegationOptions, READ_TOOLS
from app.agent.hooks import ALLOW, BEFORE_TOOL, Decision, EMPTY, using_hooks
from app.agent.permission_rules import PermissionRules, RuleGate
from app.agent.route_selection import read_only_registry
from app.agent.step_models import ModelTurn, ToolCall
from app.agent.tool_effects import effect_of
from app.config import Settings
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
from app.tools.mcp_tool_adapter import MCPToolAdapter, UserMCPToolAdapter
from app.tools.tool_loader import open_tool_registry
from app.tools.tool_registry import ToolRegistry
from app.tools.user_mcp import (
    FORBIDDEN_ENV_NAMES, MAX_SERVERS, MAX_TOOLS_PER_SERVER, UserMcpConfig, UserMcpError, UserMcpServer,
    confirm_user_mcp, load_confirmed_fingerprints, load_user_mcp, parse_user_mcp, probe_user_mcp,
    select_user_mcp_tools,
)
from loop_fakes import loop_reply

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TIME_SERVER = PROJECT_ROOT / 'app' / 'agent' / 'mcp_demo' / 'time_server.py'
ENV_SERVER = PROJECT_ROOT / 'tests' / 'mcp_env_server.py'


def _toml_str(text: str) -> str:
    return json.dumps(text)


def _write_toml(path: Path, text: str) -> Path:
    path.write_text(text, encoding='utf-8')
    return path


def _time_server_config(tmp_path: Path, *, effects: str = '', tools: str = '', timeout: int = 30) -> Path:
    lines = [
        '[servers.clock]',
        f'command = {_toml_str(sys.executable)}',
        f'args = [{_toml_str(str(TIME_SERVER))}]',
        f'timeout_seconds = {timeout}',
    ]
    if tools:
        lines.append(f'tools = {tools}')
    if effects:
        lines.append('[servers.clock.effects]')
        lines.append(effects)
    return _write_toml(tmp_path / 'mcp.toml', '\n'.join(lines) + '\n')


# --- Parsing ---

def test_parse_valid_config_and_default_cwd_uses_toml_dir(tmp_path):
    sub = tmp_path / 'conf'
    sub.mkdir()
    path = _write_toml(sub / 'mcp.toml', f'''
[servers.notes]
command = {_toml_str(sys.executable)}
args = ["-c", "pass"]
env = ["MY_NOTES_TOKEN"]
timeout_seconds = 15
tools = ["search_notes", "create_note"]
enabled = true

[servers.notes.effects]
search_notes = "read"
create_note = "external"
''')
    cfg = load_user_mcp(path, environ={'MY_NOTES_TOKEN': 'secret-value'})
    assert len(cfg) == 1
    srv = cfg.servers[0]
    assert srv.name == 'notes'
    assert srv.command == str(Path(sys.executable).resolve())
    assert srv.args == ('-c', 'pass')
    assert srv.cwd == str(sub.resolve())
    assert srv.env == ('MY_NOTES_TOKEN',)
    assert srv.timeout_seconds == 15
    assert srv.tools == ('search_notes', 'create_note')
    assert srv.effect_for('search_notes') == 'read'
    assert srv.effect_for('create_note') == 'external'
    assert srv.effect_for('other') == 'external'


@pytest.mark.parametrize('toml_text', [
    'other = 1\n',
    'servers = "bad"\n',
    f'[servers.BadName]\ncommand = {_toml_str(sys.executable)}\n',
    f'[servers.1abc]\ncommand = {_toml_str(sys.executable)}\n',
    f'[servers.a_very_long_server_name_exceeding_twenty]\ncommand = {_toml_str(sys.executable)}\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\nunknown = 1\n',
    '[servers.notes]\nargs = []\n',                                            # missing command
    '[servers.notes]\ncommand = "definitely_not_a_real_executable_xyz_999"\n',  # missing executable
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\nargs = "bad"\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\nargs = [1]\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\ncwd = "relative/path"\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\ncwd = "/definitely/nonexistent/dir/xyz"\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\nenv = ["MISSING_VAR_XYZ"]\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\ntimeout_seconds = 0\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\ntimeout_seconds = 301\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\ntimeout_seconds = true\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\ntools = []\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\nenabled = "yes"\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\n[servers.notes.effects]\nfoo = "write"\n',
    f'[servers.notes]\ncommand = {_toml_str(sys.executable)}\n[servers.notes.effects]\nfoo = "run"\n',
    '[servers.notes\ncommand = "bad toml"\n',
])
def test_parse_rejects_invalid_configs(tmp_path, toml_text):
    path = _write_toml(tmp_path / 'bad.toml', toml_text)
    with pytest.raises(UserMcpError):
        load_user_mcp(path, environ={})


@pytest.mark.parametrize('secret_name', sorted(FORBIDDEN_ENV_NAMES))
def test_parse_rejects_forbidden_env_names_without_echoing_value(tmp_path, secret_name):
    path = _write_toml(tmp_path / 'mcp.toml', f'''
[servers.notes]
command = {_toml_str(sys.executable)}
env = [{_toml_str(secret_name)}]
''')
    sentinel = 'SUPER-SECRET-VALUE-999'
    with pytest.raises(UserMcpError) as exc_info:
        load_user_mcp(path, environ={secret_name: sentinel})
    assert secret_name in str(exc_info.value)
    assert sentinel not in str(exc_info.value)


def test_parse_rejects_more_than_max_servers(tmp_path):
    servers = {f'srv_{i}': {'command': sys.executable} for i in range(MAX_SERVERS + 1)}
    with pytest.raises(UserMcpError, match=str(MAX_SERVERS)):
        parse_user_mcp({'servers': servers}, base_dir=tmp_path, environ={})


def test_settings_default_absent_vs_explicit_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('MCP_SERVERS_FILE', raising=False)
    assert Settings(_env_file=None).user_mcp_config() is None
    assert Settings(_env_file=None, mcp_servers_file='').user_mcp_config() is None
    with pytest.raises(UserMcpError, match='MCP_SERVERS_FILE'):
        Settings(_env_file=None, mcp_servers_file=str(tmp_path / 'missing.toml')).user_mcp_config()


# --- Naming, effects, truncation, non-text results ---

class _FakeClient:
    def __init__(self, result: CallToolResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict, object]] = []

    async def call_tool(self, name: str, arguments: dict, *, timeout_seconds=None) -> CallToolResult:
        self.calls.append((name, dict(arguments), timeout_seconds))
        return self.result


async def test_user_adapter_prefix_effect_description_timeout_and_nontext():
    long_desc = 'x' * 1500
    raw_tool = McpTool(
        name='write_file',
        description=long_desc,
        inputSchema={'type': 'object', 'properties': {}},
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    client = _FakeClient(CallToolResult(
        content=[
            TextContent(type='text', text='hello'),
            ImageContent(type='image', data='AA==', mimeType='image/png'),
        ],
        isError=False,
    ))
    adapter = UserMCPToolAdapter(client, raw_tool, server_name='notes', effect='external', timeout_seconds=12)
    defn = adapter.definition
    assert defn.name == 'notes__write_file'
    assert effect_of(defn) == 'external'        # readOnlyHint ignored; not confused with built-in write_file
    assert len(defn.description) == 1000

    out = await adapter.invoke({'k': 'v'})
    assert out.success is True
    assert client.calls == [('write_file', {'k': 'v'}, 12)]
    assert 'hello' in out.content
    assert '[1 image result not passed to the model]' in out.content

    # Built-in MCPToolAdapter keeps raising TypeError on non-text content.
    builtin = MCPToolAdapter(client, raw_tool)
    assert effect_of(builtin.definition) == 'read'
    with pytest.raises(TypeError):
        await builtin.invoke({})


def test_select_user_mcp_tools_filters_invalid_names_and_caps_at_30(tmp_path):
    server = UserMcpServer(name='notes', command=sys.executable, cwd=str(tmp_path))
    raw = [
        McpTool(name='bad name!', description='d', inputSchema={}),
        McpTool(name='a' * 60, description='d', inputSchema={}),       # notes__ + 60 > 64 chars
        *(McpTool(name=f'tool_{i}', description='d', inputSchema={}) for i in range(MAX_TOOLS_PER_SERVER + 5)),
    ]
    selected = select_user_mcp_tools(server, raw)
    assert len(selected.accepted) == MAX_TOOLS_PER_SERVER
    assert selected.truncated == 5
    assert 'notes__bad name!' in selected.invalid
    assert f'notes__{"a" * 60}' in selected.invalid


async def test_a_server_listing_one_tool_twice_registers_it_once(tmp_path):
    # Registering both would fail halfway, leaving the first tool bound to a closed server.
    server = UserMcpServer(name='notes', command=sys.executable, cwd=str(tmp_path))
    first = McpTool(name='find', description='first', inputSchema={})
    raw = [first, McpTool(name='find', description='second', inputSchema={}),
           McpTool(name='other', description='d', inputSchema={})]
    assert [t.description for t in select_user_mcp_tools(server, raw).accepted] == ['first', 'd']
    from app.tools.mcp_tool_loader import register_user_mcp_tools
    registry = ToolRegistry()
    client = SimpleNamespace(list_tools=AsyncMock(return_value=tuple(raw)))
    assert await register_user_mcp_tools(client, registry, server) == 2
    assert sorted(d.name for d in registry.list_definitions()) == ['notes__find', 'notes__other']


def test_select_user_mcp_tools_respects_whitelist_and_reports_missing(tmp_path):
    server = UserMcpServer(name='notes', command=sys.executable, cwd=str(tmp_path), tools=('ok', 'missing_tool'))
    raw = [McpTool(name='ok', description='d', inputSchema={}), McpTool(name='extra', description='d', inputSchema={})]
    selected = select_user_mcp_tools(server, raw)
    assert [t.name for t in selected.accepted] == ['ok']
    assert selected.missing == ('missing_tool',)


# --- Confirmation & fingerprints ---

def test_confirmation_flow_fingerprint_and_reconfirm_on_change(tmp_path):
    confirm_file = tmp_path / 'mcp_confirmed.json'
    path = _time_server_config(tmp_path, timeout=30)
    cfg = load_user_mcp(path)

    # Non-interactive without --confirm-mcp refuses.
    refused = confirm_user_mcp(cfg, confirmed=False, confirm_path=confirm_file, write=lambda _: None, interactive=False)
    assert refused is not None and '--confirm-mcp' in refused
    assert not confirm_file.exists()

    # Interactive 'n' refuses.
    refused2 = confirm_user_mcp(
        cfg, confirmed=False, confirm_path=confirm_file, write=lambda _: None, ask=lambda _: 'n', interactive=True
    )
    assert refused2 is not None and '已取消' in refused2

    # Interactive 'y' confirms and stores only {server: fingerprint}.
    shown: list[str] = []
    ok = confirm_user_mcp(
        cfg, confirmed=False, confirm_path=confirm_file, write=shown.append, ask=lambda _: 'y', interactive=True
    )
    assert ok is None
    saved = json.loads(confirm_file.read_text(encoding='utf-8'))
    assert saved == {'clock': cfg.servers[0].fingerprint()}
    assert str(TIME_SERVER) not in confirm_file.read_text(encoding='utf-8')
    assert load_confirmed_fingerprints(confirm_file) == saved

    # Unchanged config does not ask again even when non-interactive.
    asked = []
    assert confirm_user_mcp(
        cfg, confirmed=False, confirm_path=confirm_file, write=lambda _: None,
        ask=lambda p: asked.append(p) or 'n', interactive=False,
    ) is None
    assert asked == []

    # Changing timeout_seconds invalidates the fingerprint and asks again.
    path2 = _time_server_config(tmp_path, timeout=45)
    cfg2 = load_user_mcp(path2)
    refused3 = confirm_user_mcp(
        cfg2, confirmed=False, confirm_path=confirm_file, write=lambda _: None, interactive=False
    )
    assert refused3 is not None and '--confirm-mcp' in refused3


# --- Real stdio subprocesses: time_server.py and mcp_env_server.py ---

async def test_real_time_server_probe_and_service_run(tmp_path):
    path = _time_server_config(tmp_path, effects='get_current_time = "read"')
    cfg = load_user_mcp(path)

    probed = await probe_user_mcp(cfg)
    assert 'clock' in probed
    assert probed['clock'].tools == (('clock__get_current_time', 'read'),)

    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(content='', tool_calls=(ToolCall('c1', 'clock__get_current_time', {'timezone': 'UTC'}),)),
        loop_reply('Done'),
    ]
    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
        personas_dir='', user_mcp_servers=cfg,
    ) as service:
        result = await service.query_result('What time is it in UTC?', 's1')
    assert result.status is TaskStatus.COMPLETED
    assert result.content == 'Done'
    tool_result = result.steps[0].tool_results[0]
    assert tool_result.tool_name == 'clock__get_current_time'
    assert tool_result.success is True
    assert 'T' in tool_result.content


async def test_real_probe_rejects_missing_configured_tool(tmp_path):
    path = _time_server_config(tmp_path, tools='["no_such_tool"]')
    cfg = load_user_mcp(path)
    with pytest.raises(UserMcpError, match='no_such_tool'):
        await probe_user_mcp(cfg)


async def test_real_env_server_only_receives_listed_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'sk-secret-must-not-leak')
    monkeypatch.setenv('USER_MCP_ALLOWED_FLAG', 'yes')
    path = _write_toml(tmp_path / 'mcp.toml', f'''
[servers.envcheck]
command = {_toml_str(sys.executable)}
args = [{_toml_str(str(ENV_SERVER))}]
env = ["USER_MCP_ALLOWED_FLAG"]

[servers.envcheck.effects]
has_env = "read"
''')
    cfg = load_user_mcp(path)
    memory = RunMemory(RunState('t1', 'check env'))
    with using_hooks(EMPTY):
        async with open_tool_registry(memory, mcp_servers=(), user_servers=cfg.enabled_servers, include_knowledge=False) as reg:
            tool = reg.get('envcheck__has_env')
            allowed_out = await tool.invoke({'name': 'USER_MCP_ALLOWED_FLAG'})
            secret_out = await tool.invoke({'name': 'DASHSCOPE_API_KEY'})
    assert allowed_out.success is True and allowed_out.content.strip() == 'true'
    assert secret_out.success is True and secret_out.content.strip() == 'false'


# --- Runtime failure isolation & AsyncExitStack cleanup ---

async def test_runtime_unavailable_user_server_is_skipped_and_closed(tmp_path, monkeypatch):
    bad_server = UserMcpServer(
        name='broken', command=sys.executable, args=('-c', 'import sys; sys.exit(1)'),
        cwd=str(tmp_path), timeout_seconds=5,
    )
    events: list[dict] = []
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(content='', tool_calls=(ToolCall('c1', 'get_current_time', {}),)),
        loop_reply('Fallback ok'),
    ]
    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
        personas_dir='', user_mcp_servers=(bad_server,),
    ) as service:
        result = await service.query_result('hi', 's1', on_event=events.append)
    assert result.status is TaskStatus.COMPLETED
    mcp_events = [e for e in events if e.get('type') == 'mcp_server']
    assert len(mcp_events) == 1
    assert mcp_events[0]['server'] == 'broken'
    assert mcp_events[0]['result'] == 'unavailable'
    assert isinstance(mcp_events[0]['error'], str) and mcp_events[0]['error']

    # Verify CLI notice formatting for mcp_server event.
    lines: list[str] = []
    _notice_to(lines.append)(mcp_events[0])
    assert any('broken' in line and '不可用' in line for line in lines)

    # Verify subprocess/client cleanup when list_tools fails AFTER connect succeeds.
    closed = []

    @asynccontextmanager
    async def fake_connect(params, *, read_timeout_seconds=None):
        client = SimpleNamespace(list_tools=AsyncMock(side_effect=RuntimeError('list failed')))
        try:
            yield client
        finally:
            closed.append('closed')

    import app.tools.tool_loader as tl
    monkeypatch.setattr(tl.MCPClient, 'connect', classmethod(lambda cls, p, **kw: fake_connect(p, **kw)))
    unavailable: list[tuple[str, str]] = []
    memory = RunMemory(RunState('t2', 'cleanup'))
    async with open_tool_registry(
        memory, mcp_servers=(), user_servers=(bad_server,),
        on_user_server_unavailable=lambda name, err: unavailable.append((name, err)),
        include_knowledge=False,
    ) as reg:
        assert closed == ['closed']       # closed immediately on failure, before yield!
        assert unavailable == [('broken', 'RuntimeError')]
        assert reg.get('get_current_time') is not None


# --- Scope: read-only tasks, team/explore members, CLI RuleGate ---

def test_read_only_and_team_and_explore_tool_visibility():
    raw_read = McpTool(name='search', description='read tool', inputSchema={})
    raw_ext = McpTool(name='create', description='ext tool', inputSchema={})
    fake_client = _FakeClient(CallToolResult(content=[TextContent(type='text', text='ok')], isError=False))
    reg = ToolRegistry()
    reg.register(UserMCPToolAdapter(fake_client, raw_read, server_name='notes', effect='read', timeout_seconds=10))
    reg.register(UserMCPToolAdapter(fake_client, raw_ext, server_name='notes', effect='external', timeout_seconds=10))

    ro = read_only_registry(reg)
    ro_names = {d.name for d in ro.list_definitions()}
    assert 'notes__search' in ro_names
    assert 'notes__create' not in ro_names

    # Team and explore members use fixed built-in tool lists and never see user MCP tools.
    team_opts = team_options_for('team', write_root='D:/tmp', sandbox=True)
    assert 'notes__search' not in team_opts.allowed_tools
    assert 'notes__create' not in team_opts.allowed_tools
    assert 'notes__search' not in READ_TOOLS
    assert 'notes__create' not in READ_TOOLS
    assert 'notes__search' not in DelegationOptions().allowed_tools


async def test_cli_user_mcp_asks_external_and_denies_when_non_interactive(tmp_path):
    path = _time_server_config(tmp_path)      # no effects -> clock__get_current_time is external
    cfg = load_user_mcp(path)

    # 1. Without an asker (non-TTY), external user tool is denied.
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(content='', tool_calls=(ToolCall('c1', 'clock__get_current_time', {'timezone': 'UTC'}),)),
        loop_reply('Denied'),
    ]
    async with RagAgentService(
        model=model, mcp_servers=(), include_knowledge=False, memory_directory='',
        personas_dir='', user_mcp_servers=cfg, asker=None,
    ) as service:
        result = await service.query_result('time?', 's1')
    tr = result.steps[0].tool_results[0]
    assert tr.success is False
    assert 'no one can answer' in (tr.error or tr.content)

    # 2. With an asker, external user tool asks and runs when allowed.
    asked: list[str] = []

    class _AllowAsker:
        wait_seconds = 30.0

        async def ask(self, event, *, rule=None):
            asked.append(event['tool'])
            return ALLOW

    model2 = AsyncMock()
    model2.generate.side_effect = [
        ModelTurn(content='', tool_calls=(ToolCall('c1', 'clock__get_current_time', {'timezone': 'UTC'}),)),
        loop_reply('Allowed'),
    ]
    async with RagAgentService(
        model=model2, mcp_servers=(), include_knowledge=False, memory_directory='',
        personas_dir='', user_mcp_servers=cfg, asker=_AllowAsker(),
    ) as service2:
        result2 = await service2.query_result('time?', 's2')
    assert asked == ['clock__get_current_time']
    assert result2.steps[0].tool_results[0].success is True


async def test_rule_gate_ask_external_flag():
    gate_off = RuleGate(PermissionRules(()), ask_external=False, asker=None)
    assert (await gate_off.check({'tool': 'notes__create', 'effect': 'external'})).allow is True

    gate_on = RuleGate(PermissionRules(()), ask_external=True, asker=None)
    decision = await gate_on.check({'tool': 'notes__create', 'effect': 'external'})
    assert decision.allow is False


# --- CLI entry points: --show-mcp, --show-mcp --connect, --show-limits ---

async def test_cli_show_mcp_and_connect_and_show_limits(tmp_path, monkeypatch, capsys):
    from app.config import config

    # Preserve Pydantic's fields_set so monkeypatch teardown doesn't leave mcp_servers_file marked explicit.
    monkeypatch.setattr(config, '__pydantic_fields_set__', set(config.__pydantic_fields_set__))

    # 1. Absent default file -> exit 0 with Chinese message.
    monkeypatch.setattr(config, 'mcp_servers_file', '')
    code = await cli_main(parse_args(['--show-mcp']))
    assert code == 0
    assert '没有用户服务配置' in capsys.readouterr().out

    # 2. Invalid file -> exit 2 with UserMcpError.
    bad = _write_toml(tmp_path / 'bad.toml', '[servers.Bad]\ncommand = "python"\n')
    monkeypatch.setattr(config, 'mcp_servers_file', str(bad))
    code = await cli_main(parse_args(['--show-mcp']))
    assert code == 2
    assert 'UserMcpError:' in capsys.readouterr().out

    # 3. Valid file -> --show-mcp without connect, then --show-mcp --connect --confirm-mcp.
    good = _time_server_config(tmp_path, effects='get_current_time = "read"', timeout=25)
    monkeypatch.setattr(config, 'mcp_servers_file', str(good))
    confirm_file = tmp_path / 'confirmed.json'
    lines: list[str] = []
    code = await show_mcp(parse_args(['--show-mcp']), confirm_path=confirm_file, write=lines.append)
    assert code == 0
    out = '\n'.join(lines)
    assert '[clock]' in out and '未确认' in out and 'get_current_time=read' in out

    lines2: list[str] = []
    code2 = await show_mcp(
        parse_args(['--show-mcp', '--connect', '--confirm-mcp']),
        confirm_path=confirm_file, write=lines2.append, interactive=False,
    )
    assert code2 == 0
    out2 = '\n'.join(lines2)
    assert 'clock__get_current_time（read）' in out2 and '已确认' in out2

    # 4. --show-limits includes user MCP summary line.
    limits_out = show_limits(parse_args(['--show-limits']))
    assert '用户 MCP 服务：1 个' in limits_out and 'clock 25 秒' in limits_out
