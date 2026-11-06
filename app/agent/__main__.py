"""Run from the project root: python -m app.agent "Your goal", or with no goal for the interactive mode."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path

from app.agent.run_meter import RunMeter, metering
from app.config import config
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_loader import local_mcp_servers


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
                    '命令：/help 显示帮助，/new 放弃正在等待回答的任务，/exit 退出（Ctrl+D、Ctrl+Z 回车同样退出）。')


async def _read_line(prompt):
    return await asyncio.to_thread(input, prompt)


async def interactive(service, args, read_line=None, write=print) -> int:
    """Keep one service and session open across turns (D-040).

    A blocked task lives only in this process, so the next line answers it unless /new
    drops it. Each turn gets its own token and time budget. Ctrl+C stops the program; the
    service records the interrupted turn as cancelled.
    """
    read_line = read_line or _read_line
    write('Aletheia 交互模式，会话：' + args.session_id + '。' + INTERACTIVE_HELP)
    waiting = None          # task id of a blocked task waiting for an answer
    queued = args.goal      # a goal given on the command line is the first turn
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
        if command.startswith('/'):
            write(f'未知命令：{command}。' + INTERACTIVE_HELP)
            continue
        meter = RunMeter(max_total_tokens=getattr(args, 'max_tokens', None),
                         timeout_seconds=getattr(args, 'max_seconds', None))
        try:
            with metering(meter):
                result = await service.query_result(line, args.session_id, resume_task_id=waiting)
        except Exception as exc:
            # Host-side refusals (for example no resumable task); never print provider text.
            write(f'请求没有执行：{type(exc).__name__}')
            waiting = None
            continue
        report(result, write)
        usage = result.usage or {}
        if usage.get('model_calls'):
            write(f"用量：模型调用 {usage['model_calls']} 次，令牌 {usage.get('total_tokens', 0)}，"
                  f"耗时 {usage.get('seconds', 0)} 秒")
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
        team_options = team_options_for(getattr(args, 'execution_mode', 'single'), write_root, sandbox,
                                        getattr(args, 'team_timeout', None),
                                        getattr(args, 'team_acceptance', False))
        service = await stack.enter_async_context(RagAgentService(
            mcp_servers=local_mcp_servers(
                filesystem_root=args.filesystem_root or config.mcp_filesystem_root or None,
                write_root=write_root,
                tavily_api_key=config.tavily_api_key, sandbox=sandbox,
            ),
            include_knowledge=not args.no_knowledge,
            max_executions=args.max_executions, max_replans=args.max_replans,
            conversation_compressor=compressor,
            execution_mode=getattr(args, 'execution_mode', 'single'),
            team_options=team_options,
            step_timeout_seconds=step_timeout_for(sandbox, team_options),
        ))
        if args.goal is None or getattr(args, 'interactive', False):
            return await interactive(service, args)
        meter = RunMeter(max_total_tokens=getattr(args, 'max_tokens', None),
                         timeout_seconds=getattr(args, 'max_seconds', None))
        with metering(meter):
            result = await service.query_result(args.goal, args.session_id)
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
    parser.add_argument("--filesystem-root", help="Directory exposed by the filesystem MCP server")
    parser.add_argument("--write-root", help="Optional writable directory; omitted means read-only")
    parser.add_argument("--no-knowledge", action="store_true", help="Disable RAG when no knowledge store is configured")
    parser.add_argument('--execution-mode', choices=['single', 'team', 'auto'], default='single',
                        help='single: ordinary tool calling; team: the delegating team; '
                             'auto: host rules, then one model call, pick per task (D-026)')
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
