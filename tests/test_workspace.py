"""Choosing the working directory, closed folders and attachments (workspace-001, D-074)."""

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent import __main__ as cli
from app.agent.step_models import ModelTurn
from app.services.rag_agent_service import RagAgentService
from app.services.workspace import (
    WorkspaceConfirmationRequired, WorkspaceError, WorkspacePolicy, confirm_startup, system_drive,
)
from app.tools.filesystem.file_access import FileAccess
from app.tools.tool_loader import local_mcp_servers, workspace_servers

windows = pytest.mark.skipif(os.name != 'nt', reason='system drive rules are for Windows')


def drive_of(path) -> str:
    return Path(path).resolve().drive.upper()


@pytest.fixture
def off_system(monkeypatch, tmp_path):
    """Make tmp_path count as an ordinary drive."""
    monkeypatch.setenv('SystemDrive', 'Q:')


@pytest.fixture
def on_system(monkeypatch, tmp_path):
    monkeypatch.setenv('SystemDrive', drive_of(tmp_path) or 'C:')


# ---- 1. the checks --------------------------------------------------------------------------

@pytest.mark.parametrize('raw, code', [
    ('', 'invalid'), ('relative/dir', 'invalid'), ('x' * 600, 'invalid'), ('a\x00b', 'invalid'), (5, 'invalid'),
    ('\\\\server\\share\\x', 'network'), ('//server/share', 'network'),
])
def test_bad_paths_are_refused(raw, code):
    result = WorkspacePolicy().check(raw)
    assert result.problem == code and result.path is None and result.message


def test_a_missing_directory_or_a_file_is_refused(tmp_path):
    (tmp_path / 'f.txt').write_text('x', encoding='utf-8')
    assert WorkspacePolicy().check(str(tmp_path / 'nope')).problem == 'not_found'
    assert WorkspacePolicy().check(str(tmp_path / 'f.txt')).problem == 'not_found'


@windows
def test_system_places_are_refused_but_a_temp_subfolder_is_allowed(tmp_path):
    policy = WorkspacePolicy()
    windir = os.environ.get('SystemRoot', 'C:\\Windows')
    assert policy.check(windir).problem == 'system'
    assert policy.check(os.path.join(windir, 'System32')).problem == 'system'
    assert policy.check(os.environ['USERPROFILE']).problem == 'system'
    assert policy.check(os.path.join(os.environ['USERPROFILE'], 'AppData')).problem == 'system'
    import tempfile
    assert policy.check(tempfile.gettempdir()).problem == 'system'      # TEMP itself is not a workspace
    assert policy.check(str(tmp_path)).problem is None                   # a folder inside it is


@windows
def test_only_the_system_drive_root_is_refused(monkeypatch, tmp_path):
    drive = drive_of(tmp_path)
    monkeypatch.setenv('SystemDrive', drive)
    assert WorkspacePolicy().check(drive + '\\').problem == 'drive_root'
    monkeypatch.setenv('SystemDrive', 'Q:')
    result = WorkspacePolicy().check(drive + '\\')
    assert result.problem is None and result.system_drive is False


@windows
def test_the_system_drive_is_marked_and_needs_a_confirmation(on_system, tmp_path):
    policy = WorkspacePolicy()
    result = policy.check(str(tmp_path))
    assert result.system_drive is True and result.problem is None and result.path == str(tmp_path.resolve())
    with pytest.raises(WorkspaceConfirmationRequired):
        policy.require(str(tmp_path))
    assert policy.require(str(tmp_path), confirmed=True) == str(tmp_path.resolve())


def test_other_drives_need_no_confirmation(off_system, tmp_path):
    assert WorkspacePolicy().require(str(tmp_path)) == str(tmp_path.resolve())


@windows
def test_a_junction_into_the_system_drive_counts_as_the_system_drive(on_system, tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'link'
    done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
    if done.returncode != 0:
        pytest.skip('cannot create a junction here')
    result = WorkspacePolicy().check(str(link))
    assert result.system_drive is True and result.path == str(target.resolve())


def test_closed_folders_and_allowed_roots(off_system, tmp_path):
    closed, inside, other = tmp_path / 'private', tmp_path / 'private' / 'deep', tmp_path / 'other'
    for folder in (inside, other):
        folder.mkdir(parents=True)
    policy = WorkspacePolicy(deny=(closed.resolve(),))
    assert policy.check(str(closed)).problem == 'closed'
    assert policy.check(str(inside)).problem == 'closed'
    assert policy.check(str(tmp_path)).problem is None         # a closed folder inside is hidden, not refused
    protected = WorkspacePolicy(protected=(closed.resolve(),))
    assert protected.check(str(inside)).problem == 'closed'
    allowed = WorkspacePolicy(allowed_roots=(other.resolve(),))
    assert allowed.check(str(other)).problem is None
    assert allowed.check(str(tmp_path)).problem == 'not_allowed'


def test_the_policy_reads_its_lists_from_the_settings(tmp_path, monkeypatch):
    monkeypatch.delenv('ALETHEIA_TRACE_DIR', raising=False)
    settings = SimpleNamespace(conversation_storage_dir=str(tmp_path / 'conv'),
                               workspace_deny_dirs=f'{tmp_path / "a"}; {tmp_path / "b"} ;',
                               workspace_allowed_roots='')
    policy = WorkspacePolicy.from_config(settings)
    assert policy.deny == ((tmp_path / 'a').resolve(), (tmp_path / 'b').resolve())
    assert (tmp_path / 'conv').resolve() in policy.closed_folders()
    assert policy.allowed_roots == ()


@windows
def test_startup_directories_on_the_system_drive_need_a_yes(on_system, tmp_path):
    policy, out = WorkspacePolicy(), []
    paths = (str(tmp_path), None)
    assert confirm_startup(policy, paths, confirmed=True, write=out.append) is None and not out
    assert confirm_startup(policy, paths, confirmed=False, write=out.append, interactive=False)
    assert confirm_startup(policy, paths, confirmed=False, write=out.append, interactive=True, ask=lambda _: 'y') is None
    assert '已取消' in confirm_startup(policy, paths, confirmed=False, write=out.append, interactive=True,
                                     ask=lambda _: 'yes please')
    assert '系统盘' in out[0]
    assert '不存在' in confirm_startup(policy, (str(tmp_path / 'nope'),), confirmed=True)


# ---- 2. closed folders inside a working directory -----------------------------------------------

def test_the_file_tools_treat_a_closed_folder_as_missing(tmp_path):
    (tmp_path / 'private').mkdir()
    (tmp_path / 'private' / 'secret.txt').write_text('PRIVATE-WORDS', encoding='utf-8')
    (tmp_path / 'open.txt').write_text('hello PRIVATE-WORDS', encoding='utf-8')
    files = FileAccess(tmp_path, write_root=tmp_path, denied=(tmp_path / 'private',))
    with pytest.raises(PermissionError):
        files.read_file('private/secret.txt')
    with pytest.raises(PermissionError):
        files.list_directory('private')
    with pytest.raises(PermissionError):
        files.resolve_write_path('private/new.txt')
    assert [e['name'] for e in files.list_directory('.')['entries']] == ['open.txt']
    assert [m['path'] for m in files.search_files('PRIVATE-WORDS')['matches']] == ['open.txt']
    assert files.read_file('open.txt').startswith('hello')


def test_the_sandbox_masks_a_closed_folder_and_refuses_to_run_in_it(tmp_path):
    from app.tools.sandbox.executor import DockerSandbox, SandboxError
    (tmp_path / 'Private').mkdir()
    (tmp_path / 'Private' / 'run.py').write_text('print(1)', encoding='utf-8')
    (tmp_path / 'ok.py').write_text('print(1)', encoding='utf-8')
    box = DockerSandbox(tmp_path, denied=(tmp_path / 'private',))
    flags = box.protected_mounts('empty')
    assert '/work/Private:ro,size=4k' in ' '.join(flags)
    with pytest.raises(SandboxError):
        box._workspace_path('Private/run.py', kind='file')
    assert box._workspace_path('ok.py', kind='file') == '/work/ok.py'


def test_tool_servers_get_the_closed_folders(tmp_path):
    servers = local_mcp_servers(filesystem_root=tmp_path, write_root=tmp_path, sandbox=True,
                                deny=(tmp_path / 'private',))
    for server in servers:
        assert server.args[-2:] == ['--deny', str((tmp_path / 'private').resolve())]
    make = workspace_servers(writes=True, sandbox=True, deny=())
    writable, read_only = make(str(tmp_path), True), make(str(tmp_path), False)
    assert '--write-root' in writable[0].args and len(writable) == 2
    assert '--write-root' not in read_only[0].args and len(read_only) == 1
    no_writes = workspace_servers(writes=False)(str(tmp_path), True)
    assert '--write-root' not in no_writes[0].args


# ---- 3. the service ---------------------------------------------------------------------------

def outcome(text, status='succeeded', reason=None):
    return ModelTurn(json.dumps({'status': status, 'content': text, 'reason': reason}))


class Factory:
    def __init__(self):
        self.calls = []

    def __call__(self, root, writable):
        self.calls.append((root, writable))
        return ()


def service(tmp_path, turns, steps=(('Answer',),), factory=None, memory=True, **kw):
    planner, model = AsyncMock(), AsyncMock()
    planner.generate.side_effect = [json.dumps({'steps': list(s), 'reason': None}) for s in steps]
    model.generate.side_effect = list(turns)
    svc = RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                          memory_directory=(tmp_path / 'archive') if memory else '',
                          workspace_servers=factory, workspace_policy=WorkspacePolicy() if factory else None, **kw)
    return svc, planner, model


def sent_text(*mocks):
    return json.dumps([str(call) for mock in mocks for call in mock.generate.call_args_list], ensure_ascii=False)


async def test_a_chosen_directory_starts_its_own_tool_servers(off_system, tmp_path):
    factory = Factory()
    work = tmp_path / 'work'
    work.mkdir()
    svc, *_ = service(tmp_path, [outcome('a'), outcome('b')], steps=(('A',), ('B',)), factory=factory,
                      default_workspace=str(tmp_path))
    async with svc:
        await svc.query_result('q1', 's', workspace=str(work), allow_writes=False)
        await svc.query_result('q2', 's')                              # the startup directory
    assert factory.calls == [(str(work.resolve()), False)]
    assert svc.run_options()['workspace_available'] is True
    assert svc.run_options()['default_workspace'] == str(tmp_path.resolve())


async def test_refusals_happen_before_any_work(off_system, tmp_path, monkeypatch):
    svc, planner, model = service(tmp_path, [outcome('a')], factory=Factory())
    plain, *_ = service(tmp_path, [outcome('a')])
    async with svc, plain:
        with pytest.raises(WorkspaceError):
            await svc.query_result('q', 's', workspace=str(tmp_path / 'missing'))
        with pytest.raises(ValueError):
            await plain.query_result('q', 's', workspace=str(tmp_path))
        monkeypatch.setenv('SystemDrive', drive_of(tmp_path) or 'C:')
        if os.name == 'nt':
            with pytest.raises(WorkspaceConfirmationRequired):
                await svc.query_result('q', 's', workspace=str(tmp_path))
    assert planner.generate.await_count == 0 and model.generate.await_count == 0
    assert 'workspace_available' not in plain.run_options()


@windows
async def test_a_confirmed_system_drive_directory_runs(on_system, tmp_path):
    factory = Factory()
    svc, *_ = service(tmp_path, [outcome('a')], factory=factory)
    async with svc:
        result = await svc.query_result('q', 's', workspace=str(tmp_path), workspace_confirmed=True)
    assert result.content == 'a' and factory.calls == [(str(tmp_path.resolve()), True)]


async def test_a_resumed_task_keeps_its_directory_and_a_change_is_noted(off_system, tmp_path):
    factory = Factory()
    first, second = tmp_path / 'one', tmp_path / 'two'
    first.mkdir(), second.mkdir()
    svc, planner, model = service(tmp_path, [outcome('need', 'blocked', '哪个？'), outcome('done'), outcome('c')],
                                  steps=(('Ask',), ('Do',), ('C',)), factory=factory)
    async with svc:
        blocked = await svc.query_result('q', 's', workspace=str(first))
        await svc.query_result('this one', 's', resume_task_id=blocked.task_id, workspace=str(second))
        await svc.query_result('next', 's', workspace=str(second))
    assert [root for root, _ in factory.calls] == [str(first.resolve())] * 2 + [str(second.resolve())]
    text = sent_text(planner, model)
    assert text.count('working directory of this conversation has changed') >= 1
    assert str(second) not in text and str(first) not in text          # the note names no path


async def test_attachments_are_kept_in_the_archive_and_named_to_the_model(tmp_path):
    svc, planner, model = service(tmp_path, [outcome('read it')])
    other, *_ = service(tmp_path, [])
    async with svc, other:
        saved = await svc.save_attachment('s', 'C:\\Users\\me\\notes.txt', 'ATTACHED-BODY ' * 3)
        assert saved['name'] == 'notes.txt' and saved['chars'] == 42 and ':upload:' in saved['ref']
        result = await svc.query_result('summarize the file', 's', attachments=[saved['ref']])
        assert result.content == 'read it'
        text = sent_text(planner, model)
        assert saved['ref'] in text and 'notes.txt' in text and 'ATTACHED-BODY' not in text
        foreign = await other.save_attachment('other-session', 'x.txt', 'x')
        with pytest.raises(ValueError):
            await svc.query_result('q', 's', attachments=[foreign['ref']])
        with pytest.raises(ValueError):
            await svc.query_result('q', 's', attachments=['a'] * 6)
        with pytest.raises(ValueError):
            await svc.query_result('q', 's', resume_task_id='t', attachments=[saved['ref']])
        for name, body in (('', 'x'), ('a.txt', ''), ('a.txt', 'a\x00b')):
            with pytest.raises(ValueError):
                await svc.save_attachment('s', name, body)


async def test_attachments_need_the_archive(tmp_path):
    svc, *_ = service(tmp_path, [], memory=False)
    async with svc:
        with pytest.raises(ValueError, match='archive'):
            await svc.save_attachment('s', 'a.txt', 'text')


# ---- 4. the web page ---------------------------------------------------------------------------

async def test_the_web_endpoints(off_system, tmp_path):
    from test_web_api import api
    factory = Factory()
    work = tmp_path / 'work'
    work.mkdir()
    svc, *_ = service(tmp_path, [outcome('one'), outcome('two')], steps=(('A',), ('B',)), factory=factory)
    async with api(svc, write_enabled=True, write_default=False) as web:
        health = (await web.client.get('/api/health')).json()['options']
        assert health['workspace_available'] is True and health['write_available'] is True
        assert health['write_default'] is False and 'system_drive' in health
        check = (await web.client.post('/api/workspace/check', json={'path': str(work)})).json()
        assert check == {'path': str(work.resolve()), 'system_drive': False, 'problem': None, 'message': None}
        bad = (await web.client.post('/api/workspace/check', json={'path': 'relative'})).json()
        assert bad['problem'] == 'invalid'
        refused = await web.send('s1', 'q', 'r0', options={'workspace': str(tmp_path / 'missing')})
        assert refused.status_code == 400 and refused.json()['code'] == 'workspace_invalid'
        stored = await web.client.post('/api/sessions/s1/attachments', json={'name': 'a.txt', 'content': 'hello'})
        assert stored.status_code == 201
        ref = stored.json()['ref']
        response = await web.send('s1', 'q', 'r1', options={'workspace': str(work), 'write': True},
                                  attachments=[ref])
        assert response.status_code == 202
        run = await web.finished('r1')
        assert run.status == 'completed' and run.options['workspace'] == str(work.resolve())
        assert factory.calls == [(str(work.resolve()), True)]
        again = await web.send('s1', 'q', 'r1', options={'workspace': str(work), 'write': True})
        assert again.status_code == 409                                    # same request id, other content
        answer = await web.send('s1', 'q', 'r2', resume_task_id='t1', attachments=[ref])
        assert answer.status_code == 400
        binary = await web.client.post('/api/sessions/s1/attachments', json={'name': 'b', 'content': 'a\x00'})
        assert binary.status_code == 400


@windows
async def test_the_web_page_needs_the_confirmation_for_the_system_drive(on_system, tmp_path):
    from test_web_api import api
    svc, *_ = service(tmp_path, [outcome('one')], factory=Factory())
    async with api(svc, write_enabled=True) as web:
        check = (await web.client.post('/api/workspace/check', json={'path': str(tmp_path)})).json()
        assert check['system_drive'] is True
        refused = await web.send('s1', 'q', 'r1', options={'workspace': str(tmp_path)})
        assert refused.status_code == 409 and refused.json()['code'] == 'workspace_confirmation_required'
        accepted = await web.send('s1', 'q', 'r2', options={'workspace': str(tmp_path), 'workspace_confirmed': True})
        assert accepted.status_code == 202


async def test_without_the_choice_the_page_refuses_a_directory(tmp_path):
    from test_web_api import api
    svc, *_ = service(tmp_path, [])
    async with api(svc) as web:
        assert 'workspace_available' not in (await web.client.get('/api/health')).json()['options']
        assert (await web.client.post('/api/workspace/check', json={'path': str(tmp_path)})).status_code == 404
        refused = await web.send('s1', 'q', 'r1', options={'workspace': str(tmp_path)})
        assert refused.status_code == 400 and refused.json()['code'] == 'workspace_unavailable'


@pytest.mark.parametrize('host, allowed, offered', [
    ('127.0.0.1', '', True), ('0.0.0.0', '', False), ('0.0.0.0', 'ALLOWED', True)])
async def test_the_web_launcher_offers_the_choice_on_loopback_or_inside_allowed_roots(
        monkeypatch, tmp_path, host, allowed, offered):
    import uvicorn
    from app.api import __main__ as launcher
    from app.api import app as app_module
    from app.config import config
    seen = {}

    class FakeService:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class FakeServer:
        def __init__(self, cfg):
            pass

        async def serve(self):
            return None
    monkeypatch.setattr(launcher, 'RagAgentService', FakeService)
    monkeypatch.setattr(app_module, 'create_app', lambda service, **kw: seen.setdefault('app', kw))
    monkeypatch.setattr(uvicorn, 'Server', FakeServer)
    monkeypatch.setattr(uvicorn, 'Config', lambda *a, **kw: None)
    for name, value in (('web_access_token', 't'), ('longterm_memory_dsn', ''), ('mcp_filesystem_write_root', ''),
                        ('mcp_filesystem_root', ''), ('workspace_choice', True),
                        ('workspace_allowed_roots', str(tmp_path) if allowed else '')):
        monkeypatch.setattr(config, name, value)
    assert await launcher.serve(launcher.build_parser().parse_args(['--host', host])) == 0
    assert (seen['workspace_servers'] is not None) is offered
    assert seen['app']['write_enabled'] is offered and seen['app']['write_default'] is False
    assert ('write_file' in seen['team_options'].allowed_tools) is offered


async def test_a_startup_directory_on_the_system_drive_stops_the_launchers_without_a_yes(monkeypatch, tmp_path):
    from app.api import __main__ as launcher
    from app.config import config
    monkeypatch.setenv('SystemDrive', drive_of(tmp_path) or 'C:')
    monkeypatch.setattr(config, 'web_access_token', 't')
    monkeypatch.setattr('sys.stdin', SimpleNamespace(isatty=lambda: False))
    args = launcher.build_parser().parse_args(['--filesystem-root', str(tmp_path)])
    if os.name == 'nt':
        assert await launcher.serve(args) == 2


# ---- 5. the command line and the benchmark -----------------------------------------------------

def lines(*inputs):
    queue = list(inputs)

    async def read_line(prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)
    return read_line


async def test_dir_shows_and_changes_the_directory_for_the_next_tasks(off_system, tmp_path):
    factory = Factory()
    work = tmp_path / 'work'
    work.mkdir()
    svc, *_ = service(tmp_path, [outcome('a')], factory=factory, default_workspace=str(tmp_path))
    out = []
    args = SimpleNamespace(goal=None, session_id='s', user_id=None, max_tokens=None, max_seconds=None)
    async with svc:
        await cli.interactive(svc, args, lines('/dir', f'/dir {tmp_path / "missing"}', f'/dir {work}', 'q'),
                              out.append)
    text = '\n'.join(out)
    assert '工作目录：' + str(tmp_path.resolve()) in text and '不能使用这个目录' in text
    assert '工作目录已更换为：' + str(work.resolve()) in text
    assert factory.calls == [(str(work.resolve()), True)]


@windows
async def test_dir_on_the_system_drive_needs_a_typed_y(on_system, tmp_path):
    svc, *_ = service(tmp_path, [], factory=Factory())
    args = SimpleNamespace(goal=None, session_id='s', user_id=None, max_tokens=None, max_seconds=None)
    out = []
    async with svc:
        await cli.interactive(svc, args, lines(f'/dir {tmp_path}', 'no', f'/dir {tmp_path}', 'y'), out.append)
    text = '\n'.join(out)
    assert '已取消，工作目录没有更换。' in text and '工作目录已更换为：' + str(tmp_path.resolve()) in text


async def test_dir_says_when_the_service_cannot_change_it(tmp_path):
    svc, *_ = service(tmp_path, [])
    out = []
    args = SimpleNamespace(goal=None, session_id='s', user_id=None, max_tokens=None, max_seconds=None)
    async with svc:
        await cli.interactive(svc, args, lines('/dir D:\\x'), out.append)
    assert '这个服务不能更换工作目录。' in out


def test_the_bench_confirms_its_own_temp_folders(tmp_path, monkeypatch):
    import bench.run_bench as bench
    seen = []
    monkeypatch.setattr(bench, 'launch_agent',
                        lambda cmd, env, timeout: seen.append(cmd) or SimpleNamespace(stdout='', stderr='', returncode=0))
    task = {'id': 't1', 'dir': tmp_path, 'files': [], 'goal': 'g', 'expect': {}}
    try:
        bench.run_one(task, 'single', 1, 5)
    except Exception:
        pass
    assert '--confirm-system-drive' in seen[0]


def test_system_drive_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(os, 'name', 'posix')
    assert system_drive() is None
