"""Run from the project root: python -m app.agent "Your goal", or with no goal for the interactive mode."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import sys

from app.agent.persona_commands import (
    COMMANDS_HELP, PERSONA_HELP, PERSONAS_OFF, WAITING_HINT, command_notice, commands_command, memory_label,
    persona_command, persona_notice, persona_scope,
)
from app.agent.run_meter import RunMeter, metering
from app.config import config
from app.models.taskstate import TaskStatus
from app.services.personas import PersonaError
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_loader import local_mcp_servers


def trace_hooks(*roots, environ=os.environ):
    """The benchmark's trace file (D-073): with ALETHEIA_TRACE_DIR set, each run's trace is
    written there by a TraceFileHook. A directory inside a root the agent can read or write is
    refused, so the agent never sees or changes its own trace. Returns a HookSet or None."""
    directory = environ.get('ALETHEIA_TRACE_DIR')
    if not directory:
        return None
    target = Path(directory).resolve()
    for root in roots:
        if root and (target == Path(root).resolve() or Path(root).resolve() in target.parents):
            print('ALETHEIA_TRACE_DIR is inside an agent root; no trace file is written')
            return None
    from app.agent.builtin_hooks import TraceFileHook
    from app.agent.hooks import HookSet
    return HookSet([TraceFileHook(target, prefix=environ.get('ALETHEIA_TRACE_NAME', ''))])


def cache_line(usage: dict) -> str:
    """缓存命中的中文说明（D-054）：服务商没报告时直接说明，不显示成 0。"""
    if not usage.get('calls_with_cache_report'):
        return '缓存命中：服务商未报告'
    prompt, cached = usage.get('prompt_tokens', 0), usage.get('cached_prompt_tokens', 0)
    share = cached * 100 // prompt if prompt else 0
    return f'其中输入命中缓存 {cached}（{share}%）'


def team_options_for(execution_mode, write_root, sandbox=False, timeout_seconds=None):
    """Options for the delegating team (DECISIONS D-019; the only team since D-025); None when no team can run.

    auto (D-026) gets team options only with a write root and the sandbox, the one case the routing
    rules leave to the model; otherwise auto always runs single.

    Workers stay read-only unless a write root is configured; with the sandbox they may also run code
    and completion needs a passing run after the last write.
    """
    if execution_mode not in ('team', 'auto'):
        return None
    if execution_mode == 'auto' and not (write_root and sandbox):
        return None   # the routing rules allow the team only for code delivery in the sandbox (D-026)
    from app.agent.delegation import DelegationOptions, READ_TOOLS
    tools = READ_TOOLS | ({'write_file', 'edit_file'} if write_root else set()) | (
        {'run_tests', 'run_python'} if sandbox else set())
    defaults = DelegationOptions()
    return DelegationOptions(allowed_tools=frozenset(tools), require_passing_run=bool(sandbox),
                             timeout_seconds=defaults.timeout_seconds if timeout_seconds is None
                             else timeout_seconds)


def deadline_problem(sandbox, team_options):
    """With the sandbox, every task deadline must outlast one sandbox call so its own timeout
    result is seen (D-033). Refused at start-up; the deadline is no longer widened (stage 5b)."""
    if not sandbox:
        return None
    from app.agent.run_limits import RunLimits
    from app.tools.sandbox.executor import host_call_timeout_seconds
    return RunLimits.current(team=team_options).sandbox_problem(host_call_timeout_seconds())


def profile_problem():
    """A model profile file the host refuses, as text without any key; None when it is fine (4d-2)."""
    from app.models.model_profile import ModelProfileError
    try:
        config.model_profiles()
    except ModelProfileError as exc:
        return str(exc)
    return None


def profile_lines() -> str:
    """Which profile, model and window each role runs on (stage 4d-2): names and numbers only."""
    lines = ['模型档案（角色 / 档案 / 模型 / 窗口）：']
    for role, name, model, window in config.model_profiles().describe():
        lines.append(f'  {role:<9} {name:<12} {model} {window:,}')
    return chr(10).join(lines)


def user_mcp_limit_line() -> str:
    """User MCP server count and per-call timeout limits for --show-limits (D-095)."""
    from app.tools.user_mcp import UserMcpError
    try:
        cfg = config.user_mcp_config()
    except UserMcpError as exc:
        return f'UserMcpError: {exc}'
    if cfg is None or not cfg.enabled_servers:
        return '用户 MCP 服务：0 个'
    timeouts = '、'.join(f'{s.name} {s.timeout_seconds} 秒' for s in cfg.enabled_servers)
    return f'用户 MCP 服务：{len(cfg.enabled_servers)} 个（单次调用时限：{timeouts}）'


def show_limits(args) -> str:
    """The --show-limits table: every limit this command line would run under; no model call."""
    from app.agent.run_limits import RunLimits
    write_root = args.write_root or config.mcp_filesystem_write_root or None
    team = team_options_for(getattr(args, 'execution_mode', 'single'), write_root,
                            getattr(args, 'sandbox', False), getattr(args, 'team_timeout', None))
    table = RunLimits.current(team=team, max_executions=args.max_executions,
                              max_total_tokens=getattr(args, 'max_tokens', None),
                              max_seconds=getattr(args, 'max_seconds', None)).table()
    problem = profile_problem()
    return (table + chr(10) + (f'ModelProfileError: {problem}' if problem else profile_lines())
            + chr(10) + user_mcp_limit_line())


def show_permissions() -> tuple[str, int]:
    """The --show-permissions output and exit code; no model call (D-094)."""
    from app.agent.permission_rules import PermissionRulesError
    try:
        rules = config.permission_rules()
    except PermissionRulesError as exc:
        return f'PermissionRulesError: {exc}', 2
    if rules is None:
        return '没有规则文件（PERMISSIONS_FILE），行为和以前一样', 0
    return rules.format_table(), 0


async def show_mcp(args, *, confirm_path=None, write=print, ask=input, interactive=None) -> int:
    """The --show-mcp output and exit code; with --connect also confirms and probes (D-095)."""
    from app.tools.user_mcp import (
        UserMcpError, confirm_user_mcp, format_user_mcp_config, probe_user_mcp,
    )
    try:
        cfg = config.user_mcp_config()
    except UserMcpError as exc:
        write(f'UserMcpError: {exc}')
        return 2
    if cfg is None:
        write('没有用户服务配置（MCP_SERVERS_FILE），行为和以前一样')
        return 0
    probed = None
    if getattr(args, 'connect', False):
        problem = confirm_user_mcp(
            cfg,
            confirmed=getattr(args, 'confirm_mcp', False),
            confirm_path=confirm_path,
            write=write,
            ask=ask,
            interactive=interactive,
        )
        if problem:
            write(f'UserMcpError: {problem}')
            return 2
        try:
            probed = await probe_user_mcp(cfg)
        except UserMcpError as exc:
            write(f'UserMcpError: {exc}')
            return 2
    write(format_user_mcp_config(cfg, confirm_path=confirm_path, probed=probed))
    return 0


def load_output_schema(path: str) -> dict:
    """Read and validate a UTF-8 JSON Schema file for --output-schema (D-093)."""
    from app.agent.output_contract import OutputContract
    try:
        raw = Path(path).read_text(encoding='utf-8')
    except OSError as exc:
        raise ValueError(f'cannot read output schema file: {type(exc).__name__}') from exc
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError('output schema file is not valid JSON') from exc
    return OutputContract(schema).schema


def result_record(result) -> dict:
    """The machine-readable outcome written by --result-json (D-034).

    content is the model's answer (data, not instructions); everything else is host-authored.
    Calls outside the agent model made outside the request (background summaries, memory writes)
    are added as auxiliary_outside_requests when there were any (docs/cost_measurement_plan.md).
    """
    record = {'status': result.status.value, 'failure_kind': result.failure_kind,
              'user_message': result.user_message, 'content': result.content,
              'execution_count': result.execution_count, 'replan_count': result.replan_count,
              'usage': result.usage, 'steps': list(result.step_log)}
    output = getattr(result, 'output', None)
    has_contract = isinstance(result.usage, dict) and 'contract' in result.usage
    if output is not None or (has_contract and result.status is TaskStatus.COMPLETED):
        record['output'] = output
    from app.agent.run_meter import LEDGER
    outside = LEDGER.snapshot()
    if outside:
        record['auxiliary_outside_requests'] = outside
    return record


# Chinese names of the purposes a model call is booked under (docs/cost_measurement_plan.md).
PURPOSE_NAMES = {'step': '主循环', 'route': '路由', 'wrap_up': '收尾', 'lead': '团队负责人',
                 'member': '团队成员', 'reviewer': '审查员', 'explorer': '调研成员',
                 'query_rewrite': '查询改写', 'embedding': '嵌入', 'summary': '历史摘要'}


def _purpose_part(name, counts, calls_key) -> str:
    text = f"{PURPOSE_NAMES.get(name, name)} {counts.get(calls_key, 0)} 次"
    tokens = (counts.get('prompt_tokens') or 0) + (counts.get('completion_tokens') or 0)
    if tokens:
        text += f' {tokens:,} 令牌'
    elif calls_key == 'calls':
        text += '（没有报告用量）'
    cost = counts.get('estimated_cost')
    if isinstance(cost, (int, float)) and cost > 0:
        text += f'（估算费用 {cost:.4f}）'
    return text


def purpose_lines(usage) -> list[str]:
    """Where a task's tokens went: by purpose when there was more than one, and the calls outside
    the agent model (never budgeted); nothing for a plain one-purpose task."""
    usage = usage if isinstance(usage, dict) else {}
    lines = []
    by_purpose = usage.get('by_purpose') or {}
    if len(by_purpose) > 1:
        lines.append('按用途：' + '；'.join(_purpose_part(name, counts, 'model_calls')
                                         for name, counts in by_purpose.items()))
    auxiliary = usage.get('auxiliary') or {}
    if auxiliary:
        lines.append('其他调用（不计入预算）：' + '；'.join(_purpose_part(name, counts, 'calls')
                                                for name, counts in auxiliary.items()))
    return lines


def outside_line() -> str | None:
    """The process's calls outside any request, for the end of a run; None when there were none."""
    from app.agent.run_meter import LEDGER
    outside = LEDGER.snapshot()
    if not outside:
        return None
    return '任务之外的调用（后台摘要、记忆写入等）：' + '；'.join(
        _purpose_part(name, counts, 'calls') for name, counts in outside.items())


def write_result(path, result) -> None:
    target = Path(path)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(json.dumps(result_record(result), ensure_ascii=False, indent=2) + chr(10), encoding='utf-8')
    temporary.replace(target)


def report(result, write=print) -> None:
    """Print one outcome: the answer, the Chinese explanation, then the status line the bench parses."""
    write(result.content)
    if getattr(result, 'user_message', None):
        # The first line stays the English diagnostic the bench parses; this explains it (D-033).
        write(f"\n说明：{result.user_message}")
    from app.agent.provider_state import notice
    usage = getattr(result, 'usage', None) or {}
    if usage.get('unverified_changes') is True:
        from app.agent.runtime import UNVERIFIED_NOTICE
        write(f"\n{UNVERIFIED_NOTICE}")   # D-087
    warning = notice(usage.get('provider'))
    if warning:
        # Only when the provider counted less than was sent or named another model (provider-state-001).
        write(f"\n{warning}")
    for line in purpose_lines(usage):
        write(line)
    write(f"\nStatus: {result.status.value}; executions: {result.execution_count}; replans: {result.replan_count}")


INTERACTIVE_HELP = ('直接输入目标开始一个任务；任务等待回答时，直接输入回答即可继续。'
                    '命令：/help 显示帮助，/new 放弃正在等待回答的任务，/exit 退出（Ctrl+D、Ctrl+Z 回车同样退出）；'
                    '/dir 查看工作目录，/dir 路径 更换工作目录；'
                    '长期记忆：/remember 内容 记住一条（记在当前人设下，加 --shared 记成公共的），'
                    '/memories 查看，/forget 编号 删除。' + PERSONA_HELP + COMMANDS_HELP)
MEMORY_OFF = '长期记忆没有开启：在 .env 里设置 LONGTERM_MEMORY_DSN 后可用。'


async def memory_command(command, memory, user_id, write=print, *, store=None, session_id=None) -> None:
    """/remember, /memories and /forget against the user's own long-term memory (D-042). With
    personas on (store), /remember keeps the memory under the session's persona unless --shared,
    and /memories names whom each belongs to (docs/persona_memory_plan.md)."""
    name, _, rest = command.partition(' ')
    rest = rest.strip()
    if memory is None:
        write(MEMORY_OFF)
        return
    try:
        if name == '/remember':
            shared = rest == '--shared' or rest.startswith('--shared ')
            if shared:
                rest = rest[len('--shared'):].strip()
            if not rest:
                write('用法：/remember 要记住的内容；/remember --shared 内容 记成所有人设共用的')
                return
            persona_id, persona_name = (None, None)
            if store is not None and not shared:
                persona_id, persona_name = await asyncio.to_thread(persona_scope, store, user_id, session_id)
            saved = await memory.remember_explicit(user_id, rest, **({'persona_id': persona_id} if persona_id else {}))
            if store is None:
                write(f'已记住，编号 {saved.memory_id}。')
            else:
                owner = f'属于「{persona_name}」' if persona_id else '公共的'
                write(f'已记住（{owner}），编号 {saved.memory_id}。')
        elif name == '/memories':
            records = await memory.list_memories(user_id)
            if not records:
                write('还没有长期记忆。')
            for record in records:
                if store is None:
                    write(f'{record.memory_id}  [{record.category.value}]  {record.content}')
                else:
                    label = memory_label(store, user_id, getattr(record, 'persona_id', None))
                    write(f'{record.memory_id}  [{record.category.value}]  [{label}]  {record.content}')
        else:
            if not rest:
                write('用法：/forget 编号（编号见 /memories）')
                return
            write('已删除。' if await memory.forget(user_id, rest) else '没有这条记忆。')
    except Exception as exc:
        # Type name only: store and index errors may carry connection details.
        write(f'长期记忆操作失败：{type(exc).__name__}')


def _persona_store(service):
    """The service's persona store, or None when personas are off (or the service is a stand-in)."""
    from app.services.personas import PersonaStore
    store = getattr(service, 'personas', None)
    return store if isinstance(store, PersonaStore) else None


def _notice_to(write):
    """An on_event that prints the task's persona line (docs/persona_plan.md), its personal
    command's line (docs/personal_commands_plan.md), project instructions (D-096), and
    unavailable user MCP servers (D-095)."""
    def on_event(event):
        if event.get('type') == 'persona':
            line = persona_notice(event)
            if line:
                write(line)
        elif event.get('type') == 'instructions':
            from app.services.project_instructions import instructions_notice
            line = instructions_notice(event)
            if line:
                write(line)
        elif event.get('type') == 'command':
            write(command_notice(event))
        elif event.get('type') == 'mcp_server' and event.get('result') == 'unavailable':
            write(f"用户服务 {event.get('server')} 这次没能启动（{event.get('error', 'Error')}），它的工具不可用")
    return on_event


async def prompt_instructions(service, workspace, *, declined: set, read_line, write=print,
                              interactive: bool | None = None) -> None:
    """In an interactive terminal, ask whether to trust an unconfirmed/changed project instructions file."""
    is_tty = sys.stdin.isatty() if interactive is None else interactive
    if not is_tty:
        return
    status_fn = getattr(service, 'instructions_status', None)
    if not callable(status_fn):
        return
    found = status_fn(workspace)
    if found is None or found.status not in ('unconfirmed', 'changed'):
        return
    target = workspace or (service.run_options().get('default_workspace')
                           if hasattr(service, 'run_options') else None)
    key = (os.path.normcase(str(Path(target).resolve())) if target else '', found.name)
    if key in declined:
        return
    from app.services.project_instructions import InstructionsChanged, MAX_INSTRUCTION_CHARS
    if found.status == 'changed':
        write(f'项目说明 {found.name}（{found.chars} 字）内容已更改，需要重新确认。')
    else:
        write(f'这个目录有项目说明 {found.name}（{found.chars} 字），还没确认。')
    try:
        answer = (await read_line('输入 v 显示全文，y 信任，其他任意输入这次运行不再问：')).strip().lower()
    except EOFError:
        answer = ''
    if answer == 'v':
        write(found.text)
        if found.truncated:
            write(f'（超过 {MAX_INSTRUCTION_CHARS} 字的部分已截断，不会发给模型）')
        try:
            answer = (await read_line('信任这份项目说明吗？输入 y 信任，其他任意输入这次运行不再问：')).strip().lower()
        except EOFError:
            answer = ''
    if answer == 'y':
        try:
            service.confirm_instructions(workspace, found.digest)
            write(f'已信任项目说明 {found.name}。')
        except InstructionsChanged:
            write(f'项目说明 {found.name} 刚刚发生变化，未确认。')
    else:
        declined.add(key)
        write(f'本次运行不再询问 {found.name}。')


def personal_command(store, user_id, session_id, line, persona_id=None):
    """The name of the personal command a line runs, or None: the line starts with /name and the
    persona (the given one, else the session's) has a command of that name."""
    from app.services.personal_commands import split_line
    parts = split_line(line)
    if store is None or parts is None:
        return None
    try:
        chosen = persona_id or store.session_persona_id(user_id, session_id)
        return parts[0] if store.commands.has(user_id, chosen, parts[0]) else None
    except (PersonaError, OSError):
        return None


from app.agent.terminal_input import TerminalReader

_terminal_reader = TerminalReader()


async def _read_line(prompt):
    return await _terminal_reader.read_line(prompt)


async def dir_command(command, state, service, read_line, write=print, waiting=None, *,
                      declined=None, is_tty=None) -> None:
    """/dir shows the working directory; /dir PATH changes it for the next tasks (workspace-001).
    A directory on the system drive needs a typed 'y' (D-074)."""
    offered = service.run_options()
    if not offered.get('workspace_available'):
        write('这个服务不能更换工作目录。')
        return
    raw = command[len('/dir'):].strip()
    if not raw:
        current = state.get('workspace') or offered.get('default_workspace')
        write('工作目录：' + (current or '（没有设置，文件工具不可用）'))
        return
    from app.services.workspace import SYSTEM_DRIVE_WARNING, system_drive
    result = service.check_workspace(raw)
    if result.problem is not None:
        write('不能使用这个目录：' + result.message)
        return
    if result.system_drive:
        write(SYSTEM_DRIVE_WARNING.format(drive=system_drive(), path=result.path))
        try:
            answer = await read_line('仍然使用这个目录吗？输入 y 确认，其他任意输入取消：')
        except EOFError:
            answer = ''
        if answer.strip().lower() != 'y':
            write('已取消，工作目录没有更换。')
            return
    state.update(workspace=result.path, confirmed=result.system_drive)
    write('工作目录已更换为：' + result.path + ('（等待回答的任务仍用原来的目录）' if waiting else ''))
    await prompt_instructions(service, result.path, declined=declined if declined is not None else set(),
                              read_line=read_line, write=write, interactive=is_tty)


async def interactive(service, args, read_line=None, write=print, memory=None, *, is_tty=None) -> int:
    """Keep one service and session open across turns (D-040).

    A blocked task lives only in this process, so the next line answers it unless /new
    drops it. Each turn gets its own token and time budget. Ctrl+C stops the program; the
    service records the interrupted turn as cancelled.
    """
    read_line = read_line or _read_line
    user_id = getattr(args, 'user_id', None)
    write('Aletheia 交互模式，会话：' + args.session_id + '。' + INTERACTIVE_HELP)
    waiting = None          # task id of a blocked task waiting for an answer
    queued = args.goal      # a goal given on the command line is the first turn
    place = {}              # the working directory chosen with /dir (workspace-001)
    declined = set()        # (normalized_workspace, filename) declined for this run (D-096)
    await prompt_instructions(service, None, declined=declined, read_line=read_line, write=write,
                              interactive=is_tty)
    while True:
        if queued is not None:
            line, queued = queued, None
        else:
            try:
                line = await read_line('回答> ' if waiting else '> ')
            except EOFError:
                write('')
                return 0
        command = line.strip()
        if not command:
            continue
        if command in ('/exit', '/quit'):
            return 0
        if command == '/help':
            write(INTERACTIVE_HELP)
            continue
        if command == '/new':
            write('已放弃正在等待回答的任务。' if waiting else '当前没有等待回答的任务。')
            waiting = None
            continue
        if command == '/dir' or command.startswith('/dir '):
            await dir_command(command, place, service, read_line, write, waiting,
                              declined=declined, is_tty=is_tty)
            continue
        if command.split(' ', 1)[0] in ('/remember', '/memories', '/forget'):
            await memory_command(command, memory, user_id, write,
                                 store=_persona_store(service), session_id=args.session_id)
            continue
        if command.split(' ', 1)[0] in ('/persona', '/btw'):
            await persona_command(command, service, user_id, args.session_id, write, waiting,
                                  memory=memory, read_line=read_line)
            continue
        if command.split(' ', 1)[0] == '/commands':
            await commands_command(command, _persona_store(service), user_id, args.session_id, write, read_line)
            continue
        run_command = None
        if command.startswith('/'):
            run_command = await asyncio.to_thread(personal_command, _persona_store(service), user_id,
                                                  args.session_id, command)
            if run_command is None:
                write(f'未知命令：{command}。' + INTERACTIVE_HELP)
                continue
            if waiting:
                write(WAITING_HINT)
                continue
        meter = RunMeter(max_total_tokens=getattr(args, 'max_tokens', None),
                         timeout_seconds=getattr(args, 'max_seconds', None))
        try:
            with metering(meter):
                chosen = ({'workspace': place['workspace'], 'workspace_confirmed': place['confirmed']}
                          if place.get('workspace') and waiting is None else {})
                if run_command is not None:
                    chosen['command'] = run_command
                result = await service.query_result(line, args.session_id, user_id=user_id,
                                                    resume_task_id=waiting, on_event=_notice_to(write),
                                                    **chosen)
        except PersonaError as exc:
            write('请求没有执行：' + exc.message)     # a persona file the user must fix
            waiting = None
            continue
        except Exception as exc:
            # Host-side refusals (for example no resumable task); never print provider text.
            write(f'请求没有执行：{type(exc).__name__}')
            waiting = None
            continue
        report(result, write)
        usage = result.usage or {}
        if usage.get('model_calls'):
            write(f"用量：模型调用 {usage['model_calls']} 次，令牌 {usage.get('total_tokens', 0)}，"
                  f"{cache_line(usage)}，耗时 {usage.get('seconds', 0)} 秒")
        if result.status is TaskStatus.BLOCKED:
            waiting = result.task_id
            write('任务在等待你的回答：直接输入回答继续，输入 /new 放弃。')
        else:
            waiting = None


def sandbox_problem(write_root, sandbox):
    """Reason the sandbox cannot be used, checked before any model call; None when fine."""
    if not sandbox:
        return None
    if not write_root:
        return '--sandbox requires --write-root (the sandbox workspace)'
    from app.tools.sandbox.executor import DockerSandbox, SandboxError
    try:
        if not DockerSandbox(write_root).available():
            return 'sandbox image not available (is Docker running? build: docker build -t aletheia-sandbox:py313 deploy/sandbox)'
    except SandboxError as exc:
        return f'sandbox workspace invalid: {exc}'
    return None


async def main(args) -> int:
    if getattr(args, 'show_limits', False):
        print(show_limits(args))
        return 0
    if getattr(args, 'show_permissions', False):
        text, code = show_permissions()
        print(text)
        return code
    if getattr(args, 'show_mcp', False):
        return await show_mcp(args)
    output_schema = None
    if getattr(args, 'output_schema', None):
        try:
            output_schema = load_output_schema(args.output_schema)
            if getattr(args, 'execution_mode', 'single') == 'team':
                raise ValueError('output_schema is not supported in team mode')
        except ValueError as exc:
            print(f'OutputSchemaError: {exc}')
            return 2
    async with AsyncExitStack() as stack:
        compressor = None
        if getattr(args, 'compress_history', False):
            if not config.conversation_storage_dir:
                raise ValueError('--compress-history requires CONVERSATION_STORAGE_DIR')
            from app.core.dependencies import open_conversation_compressor
            compressor = await stack.enter_async_context(open_conversation_compressor())
        write_root = args.write_root or config.mcp_filesystem_write_root or None
        sandbox = getattr(args, 'sandbox', False)
        problem = sandbox_problem(write_root, sandbox)
        if problem:
            print(f'SandboxUnavailableError: {problem}')
            return 2
        filesystem_root = args.filesystem_root or config.mcp_filesystem_root or None
        # Every directory passes the same checks; one on the system drive needs confirming (D-074).
        from app.services.workspace import WorkspacePolicy, confirm_startup
        policy = WorkspacePolicy.from_config()
        problem = confirm_startup(policy, (filesystem_root, write_root),
                                  confirmed=getattr(args, 'confirm_system_drive', False))
        if problem:
            print(f'WorkspaceError: {problem}')
            return 2
        memory = None
        if config.longterm_memory_dsn:
            from app.services.longterm_memory_setup import open_longterm_memory
            memory = await stack.enter_async_context(open_longterm_memory())
        # A user id scopes long-term memory; the local CLI user is 'local' unless given.
        args.user_id = getattr(args, 'user_id', None) or ('local' if memory is not None else None)
        team_options = team_options_for(getattr(args, 'execution_mode', 'single'), write_root, sandbox,
                                        getattr(args, 'team_timeout', None))
        problem = deadline_problem(sandbox, team_options)
        if problem:
            print(f'DeadlineError: {problem}')
            return 2
        problem = profile_problem()
        if problem:
            print(f'ModelProfileError: {problem}')
            return 2
        from app.agent.permission_rules import PermissionRulesError
        from app.agent.terminal_input import TerminalAsker
        try:
            permission_rules = config.permission_rules()
        except PermissionRulesError as exc:
            print(f'PermissionRulesError: {exc}')
            return 2
        from app.tools.user_mcp import UserMcpError, confirm_user_mcp, probe_user_mcp
        try:
            user_mcp = config.user_mcp_config()
        except UserMcpError as exc:
            print(f'UserMcpError: {exc}')
            return 2
        user_mcp_servers = user_mcp.enabled_servers if user_mcp else ()
        if user_mcp_servers:
            problem = confirm_user_mcp(user_mcp, confirmed=getattr(args, 'confirm_mcp', False))
            if problem:
                print(f'UserMcpError: {problem}')
                return 2
            try:
                await probe_user_mcp(user_mcp)
            except UserMcpError as exc:
                print(f'UserMcpError: {exc}')
                return 2
        asker = (TerminalAsker(_terminal_reader, config.web_approval_timeout_seconds)
                 if (permission_rules or user_mcp_servers) and sys.stdin.isatty() else None)
        startup_deny = (*policy.closed_folders(),
                        *(permission_rules.deny_paths_for(filesystem_root, write_root) if permission_rules else ()))
        from app.services.project_instructions import InstructionsTrust
        instructions_trust = None if getattr(args, 'no_instructions', False) else InstructionsTrust()
        from app.tools.tool_loader import workspace_servers
        service = await stack.enter_async_context(RagAgentService(
            hooks=trace_hooks(filesystem_root, write_root),
            mcp_servers=local_mcp_servers(
                filesystem_root=filesystem_root,
                write_root=write_root,
                tavily_api_key=config.tavily_api_key, sandbox=sandbox, deny=startup_deny,
            ),
            # /dir in the interactive mode; a chosen directory is writable only when a write root was given.
            workspace_servers=(workspace_servers(tavily_api_key=config.tavily_api_key, sandbox=sandbox,
                                                 writes=bool(write_root), deny=policy.closed_folders(),
                                                 permission_rules=permission_rules)
                               if config.workspace_choice else None),
            workspace_policy=policy, default_workspace=write_root or filesystem_root,
            include_knowledge=not args.no_knowledge,
            max_executions=args.max_executions,
            conversation_compressor=compressor,
            execution_mode=getattr(args, 'execution_mode', 'single'),
            team_options=team_options,
            longterm_memory=memory, longterm_recall_limit=config.longterm_recall_limit,
            single_runner=getattr(args, 'single_runner', 'loop'),
            delegate=getattr(args, 'delegate', False),
            permission_rules=permission_rules,
            asker=asker,
            user_mcp_servers=user_mcp_servers,
            instructions_trust=instructions_trust,
        ))
        if getattr(args, 'trust_instructions', False) and instructions_trust is not None:
            found = service.instructions_status()
            if found is not None and found.status in ('unconfirmed', 'changed'):
                service.confirm_instructions(None, found.digest)
        persona_id, store = None, getattr(service, 'personas', None)
        if getattr(args, 'persona', None):
            if store is None:
                print(PERSONAS_OFF)
                return 2
            try:
                persona_id = store.find(args.user_id, args.persona).persona_id
            except PersonaError as exc:
                print(exc.message)
                return 2
        if args.goal is None or getattr(args, 'interactive', False):
            if persona_id is not None:       # the first persona of this session (docs/persona_plan.md)
                store.bind(args.user_id, args.session_id, persona_id)
            code = await interactive(service, args, memory=memory)
            if outside_line():
                print(outside_line())
            return code
        meter = RunMeter(max_total_tokens=getattr(args, 'max_tokens', None),
                         timeout_seconds=getattr(args, 'max_seconds', None))
        try:
            with metering(meter):
                has_instructions = instructions_trust is not None and bool(write_root or filesystem_root)
                extra = {'persona': persona_id, 'on_event': _notice_to(print)} if store is not None else (
                    {'on_event': _notice_to(print)} if (user_mcp_servers or has_instructions) else {}
                )
                # A goal that names one of the persona's commands runs it (docs/personal_commands_plan.md).
                run_command = personal_command(_persona_store(service), args.user_id, args.session_id,
                                               args.goal, persona_id)
                if run_command is not None:
                    extra['command'] = run_command
                if output_schema is not None:
                    extra['output_schema'] = output_schema
                result = await service.query_result(args.goal, args.session_id, user_id=args.user_id, **extra)
        except PersonaError as exc:
            print('PersonaError: ' + exc.message)
            return 2
    if getattr(args, 'result_json', None):
        write_result(args.result_json, result)
    report(result)
    return 0 if result.status is TaskStatus.COMPLETED else (2 if result.status is TaskStatus.BLOCKED else 1)


def _positive(kind):
    def parse(text):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f'not a number: {text!r}') from None
        if not value > 0 or value == float('inf'):
            raise argparse.ArgumentTypeError('must be positive and finite')
        return value
    return parse


def build_parser():
    parser = argparse.ArgumentParser(description="Aletheia agent: one agent loop per task")
    parser.add_argument("goal", nargs="?",
                        help="The task; omit it (or pass --interactive) for the interactive mode")
    parser.add_argument('-i', '--interactive', action='store_true',
                        help='Keep the session open: run several tasks and answer blocked ones (D-040)')
    parser.add_argument("--session-id", default="cli")
    parser.add_argument('--user-id', help="Scope for long-term memory and archives (default 'local' when "
                                          'long-term memory is on; D-042)')
    parser.add_argument("--filesystem-root", help="Directory exposed by the filesystem MCP server")
    parser.add_argument("--write-root", help="Optional writable directory; omitted means read-only")
    parser.add_argument('--confirm-system-drive', action='store_true',
                        help='Confirm a --filesystem-root/--write-root on the system drive (C:) without a prompt (D-074)')
    parser.add_argument('--confirm-mcp', action='store_true',
                        help='Confirm user MCP servers from MCP_SERVERS_FILE without a prompt (D-095)')
    parser.add_argument('--trust-instructions', action='store_true',
                        help="Trust the current working directory's project instructions file for this run (D-096)")
    parser.add_argument('--no-instructions', action='store_true',
                        help='Do not read project instructions files from the working directory (D-096)')
    parser.add_argument("--no-knowledge", action="store_true", help="Disable RAG when no knowledge store is configured")
    parser.add_argument('--execution-mode', choices=['single', 'team', 'auto'], default='single',
                        help='single: ordinary tool calling; team: the delegating team; '
                             'auto: host rules, then one model call, pick per task (D-026)')
    parser.add_argument('--single-runner', choices=['loop'], default='loop',
                        help="How single mode runs: one agent loop (D-072); plan mode was removed (D-076)")
    parser.add_argument('--delegate', action='store_true',
                        help='Let the single loop delegate wide read-only research to fresh members '
                             '(off by default, loop-delegate-001)')
    parser.add_argument('--sandbox', action='store_true',
                        help='Let the agent run workspace code/tests in the Docker sandbox (needs --write-root)')
    parser.add_argument('--team-timeout', type=float, help='Team time budget in seconds (default 180)')
    parser.add_argument("--max-executions", type=int, default=16)
    parser.add_argument('--compress-history', action='store_true',
                        help='Generate archived stage summaries using the existing local compressor')
    parser.add_argument('--max-tokens', type=_positive(int),
                        help='Stop between model calls once this many tokens were used (D-034)')
    parser.add_argument('--max-seconds', type=_positive(float), help='Total time budget for the task in seconds (D-034)')
    parser.add_argument('--result-json', help='Also write the outcome, usage and step log as JSON here')
    parser.add_argument('--output-schema', help='UTF-8 JSON Schema file that the final answer must match (D-093)')
    parser.add_argument('--persona', help='Persona (name or id) for this task; in the interactive mode, '
                                          "the session's persona from the start (docs/persona_plan.md)")
    parser.add_argument('--show-limits', action='store_true',
                        help='Print every limit and deadline this command line would use, then exit (no model call)')
    parser.add_argument('--show-permissions', action='store_true',
                        help='Print permission rules from PERMISSIONS_FILE, then exit (no model call)')
    parser.add_argument('--show-mcp', action='store_true',
                        help='Print user MCP servers from MCP_SERVERS_FILE, then exit (no model call)')
    parser.add_argument('--connect', action='store_true',
                        help='With --show-mcp, confirm and probe each enabled server to list its tools')
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.connect and not args.show_mcp:
        parser.error('--connect can only be used with --show-mcp')
    if args.trust_instructions and args.no_instructions:
        parser.error('--trust-instructions and --no-instructions cannot be used together')
    if (args.goal is None or args.interactive) and args.result_json:
        parser.error('--result-json needs a single goal, not the interactive mode')
    if (args.goal is None or args.interactive) and args.output_schema:
        parser.error('--output-schema needs a single goal, not the interactive mode')
    return args


if __name__ == "__main__":
    from app.core.logging_setup import configure_logging
    configure_logging()
    try:
        raise SystemExit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
