"""Run from the project root: python -m app.agent "Your goal", or with no goal for the interactive mode."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path

from app.agent.run_meter import RunMeter, metering
from app.config import config
from app.models.taskstate import TaskStatus
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


def team_options_for(execution_mode, write_root, sandbox=False, timeout_seconds=None, acceptance=False):
    """Options for the delegating team (DECISIONS D-019; the only team since D-025); None when no team can run.

    auto (D-026) gets team options only with a write root and the sandbox, the one case the routing
    rules leave to the model; otherwise auto always runs single.

    Workers stay read-only unless a write root is configured; with the sandbox they may also run code
    and completion needs a passing run after the last write. acceptance opts in to independent
    acceptance tests, off by default because weak models write wrong ones (DECISIONS D-022).
    """
    if execution_mode not in ('team', 'auto'):
        return None
    if execution_mode == 'auto' and not (write_root and sandbox):
        return None   # the routing rules allow the team only for code delivery in the sandbox (D-026)
    from app.agent.team_v2 import DelegationOptions, READ_TOOLS
    tools = READ_TOOLS | ({'write_file'} if write_root else set()) | (
        {'run_tests', 'run_python'} if sandbox else set())
    defaults = DelegationOptions()
    return DelegationOptions(allowed_tools=frozenset(tools), require_passing_run=bool(sandbox),
                             acceptance_tests=bool(sandbox and acceptance),
                             timeout_seconds=defaults.timeout_seconds if timeout_seconds is None
                             else timeout_seconds)


def step_timeout_for(sandbox, team_options):
    """A step must outlast one sandbox call, so the sandbox's own timeout result is seen (D-033).

    None keeps the configured default (the service still widens it for the team).
    """
    if not sandbox:
        return None
    from app.tools.sandbox.executor import host_call_timeout_seconds
    seconds = max(config.agent_step_timeout_seconds, host_call_timeout_seconds() + 30)
    if team_options is not None:
        seconds = max(seconds, team_options.timeout_seconds + 5)
    return seconds


def result_record(result) -> dict:
    """The machine-readable outcome written by --result-json (D-034).

    content is the model's answer (data, not instructions); everything else is host-authored.
    """
    return {'status': result.status.value, 'failure_kind': result.failure_kind,
            'user_message': result.user_message, 'content': result.content,
            'execution_count': result.execution_count, 'replan_count': result.replan_count,
            'usage': result.usage, 'steps': list(result.step_log)}


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
    write(f"\nStatus: {result.status.value}; executions: {result.execution_count}; replans: {result.replan_count}")


INTERACTIVE_HELP = ('直接输入目标开始一个任务；任务等待回答时，直接输入回答即可继续。'
                    '命令：/help 显示帮助，/new 放弃正在等待回答的任务，/exit 退出（Ctrl+D、Ctrl+Z 回车同样退出）；'
                    '/dir 查看工作目录，/dir 路径 更换工作目录；'
                    '长期记忆：/remember 内容 记住一条，/memories 查看，/forget 编号 删除。')
MEMORY_OFF = '长期记忆没有开启：在 .env 里设置 LONGTERM_MEMORY_DSN 后可用。'


async def memory_command(command, memory, user_id, write=print) -> None:
    """/remember, /memories and /forget against the user's own long-term memory (D-042)."""
    name, _, rest = command.partition(' ')
    rest = rest.strip()
    if memory is None:
        write(MEMORY_OFF)
        return
    try:
        if name == '/remember':
            if not rest:
                write('用法：/remember 要记住的内容')
                return
            saved = await memory.remember_explicit(user_id, rest)
            write(f'已记住，编号 {saved.memory_id}。')
        elif name == '/memories':
            records = await memory.list_memories(user_id)
            if not records:
                write('还没有长期记忆。')
            for record in records:
                write(f'{record.memory_id}  [{record.category.value}]  {record.content}')
        else:
            if not rest:
                write('用法：/forget 编号（编号见 /memories）')
                return
            write('已删除。' if await memory.forget(user_id, rest) else '没有这条记忆。')
    except Exception as exc:
        # Type name only: store and index errors may carry connection details.
        write(f'长期记忆操作失败：{type(exc).__name__}')


async def _read_line(prompt):
    return await asyncio.to_thread(input, prompt)


async def dir_command(command, state, service, read_line, write=print, waiting=None) -> None:
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


async def interactive(service, args, read_line=None, write=print, memory=None) -> int:
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
            await dir_command(command, place, service, read_line, write, waiting)
            continue
        if command.split(' ', 1)[0] in ('/remember', '/memories', '/forget'):
            await memory_command(command, memory, user_id, write)
            continue
        if command.startswith('/'):
            write(f'未知命令：{command}。' + INTERACTIVE_HELP)
            continue
        meter = RunMeter(max_total_tokens=getattr(args, 'max_tokens', None),
                         timeout_seconds=getattr(args, 'max_seconds', None))
        try:
            with metering(meter):
                chosen = ({'workspace': place['workspace'], 'workspace_confirmed': place['confirmed']}
                          if place.get('workspace') and waiting is None else {})
                result = await service.query_result(line, args.session_id, user_id=user_id,
                                                    resume_task_id=waiting, **chosen)
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
                                        getattr(args, 'team_timeout', None),
                                        getattr(args, 'team_acceptance', False))
        from app.tools.tool_loader import workspace_servers
        service = await stack.enter_async_context(RagAgentService(
            hooks=trace_hooks(filesystem_root, write_root),
            mcp_servers=local_mcp_servers(
                filesystem_root=filesystem_root,
                write_root=write_root,
                tavily_api_key=config.tavily_api_key, sandbox=sandbox, deny=policy.closed_folders(),
            ),
            # /dir in the interactive mode; a chosen directory is writable only when a write root was given.
            workspace_servers=(workspace_servers(tavily_api_key=config.tavily_api_key, sandbox=sandbox,
                                                 writes=bool(write_root), deny=policy.closed_folders())
                               if config.workspace_choice else None),
            workspace_policy=policy, default_workspace=write_root or filesystem_root,
            include_knowledge=not args.no_knowledge,
            max_executions=args.max_executions, max_replans=args.max_replans,
            conversation_compressor=compressor,
            execution_mode=getattr(args, 'execution_mode', 'single'),
            team_options=team_options,
            step_timeout_seconds=step_timeout_for(sandbox, team_options),
            longterm_memory=memory, longterm_recall_limit=config.longterm_recall_limit,
            single_runner=getattr(args, 'single_runner', 'loop'),
        ))
        if args.goal is None or getattr(args, 'interactive', False):
            return await interactive(service, args, memory=memory)
        meter = RunMeter(max_total_tokens=getattr(args, 'max_tokens', None),
                         timeout_seconds=getattr(args, 'max_seconds', None))
        with metering(meter):
            result = await service.query_result(args.goal, args.session_id, user_id=args.user_id)
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
    parser = argparse.ArgumentParser(description="Aletheia plan/execute/replan runtime")
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
    parser.add_argument("--no-knowledge", action="store_true", help="Disable RAG when no knowledge store is configured")
    parser.add_argument('--execution-mode', choices=['single', 'team', 'auto'], default='single',
                        help='single: ordinary tool calling; team: the delegating team; '
                             'auto: host rules, then one model call, pick per task (D-026)')
    parser.add_argument('--single-runner', choices=['plan', 'loop'], default='loop',
                        help="How single mode runs: one agent loop (default, D-072), or plan then steps")
    parser.add_argument('--sandbox', action='store_true',
                        help='Let the agent run workspace code/tests in the Docker sandbox (needs --write-root)')
    parser.add_argument('--team-timeout', type=float, help='Team time budget in seconds (default 180)')
    parser.add_argument('--team-acceptance', action='store_true',
                        help='Team V2 with --sandbox: independent acceptance tests decide completion (D-020/D-022)')
    parser.add_argument("--max-executions", type=int, default=16)
    parser.add_argument("--max-replans", type=int, default=2)
    parser.add_argument('--compress-history', action='store_true',
                        help='Generate archived stage summaries using the existing local compressor')
    parser.add_argument('--max-tokens', type=_positive(int),
                        help='Stop between model calls once this many tokens were used (D-034)')
    parser.add_argument('--max-seconds', type=_positive(float), help='Total time budget for the task in seconds (D-034)')
    parser.add_argument('--result-json', help='Also write the outcome, usage and step log as JSON here')
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.goal is None or args.interactive) and args.result_json:
        parser.error('--result-json needs a single goal, not the interactive mode')
    return args


if __name__ == "__main__":
    from app.core.logging_setup import configure_logging
    configure_logging()
    try:
        raise SystemExit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
