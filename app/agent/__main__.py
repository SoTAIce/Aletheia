"""Run from the project root: python -m app.agent \"Your goal\"."""

import argparse
import asyncio
from contextlib import AsyncExitStack

from app.config import config
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_loader import local_mcp_servers


def team_options_for(execution_mode, write_root, sandbox=False, timeout_seconds=None):
    """Team stays read-only unless a write root is configured; then workers may write files.

    With the sandbox, workers may also run code and finish needs a passing run; write -> test -> fix
    loops need larger team budgets than the read-only defaults (team-recovery-001).
    """
    if execution_mode != 'team' or (not write_root and timeout_seconds is None):
        return None
    from app.agent.team.models import TeamOptions
    defaults = TeamOptions()
    tools = defaults.allowed_tools
    if write_root:
        tools = tools | {'write_file'}
    if sandbox:
        tools = tools | {'run_tests', 'run_python'}
    budgets = SANDBOX_TEAM_BUDGETS if sandbox else {}
    return TeamOptions(allowed_tools=tools, require_passing_run=bool(sandbox),
                       timeout_seconds=defaults.timeout_seconds if timeout_seconds is None else timeout_seconds,
                       **budgets)


SANDBOX_TEAM_BUDGETS = {'max_tool_calls': 96, 'max_model_calls': 120, 'max_estimated_tokens': 1_500_000}


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
            team_options=team_options_for(getattr(args, 'execution_mode', 'single'), write_root, sandbox,
                                          getattr(args, 'team_timeout', None)),
        ))
        result = await service.query_result(args.goal, args.session_id)
    print(result.content)
    print(f"\nStatus: {result.status.value}; executions: {result.execution_count}; replans: {result.replan_count}")
    return 0 if result.status is TaskStatus.COMPLETED else (2 if result.status is TaskStatus.BLOCKED else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aletheia plan/execute/replan runtime")
    parser.add_argument("goal")
    parser.add_argument("--session-id", default="cli")
    parser.add_argument("--filesystem-root", help="Directory exposed by the filesystem MCP server")
    parser.add_argument("--write-root", help="Optional writable directory; omitted means read-only")
    parser.add_argument("--no-knowledge", action="store_true", help="Disable RAG when no knowledge store is configured")
    parser.add_argument('--execution-mode', choices=['single', 'team'], default='single',
                        help='Use ordinary tool calling or an independently led reviewed team')
    parser.add_argument('--sandbox', action='store_true',
                        help='Let the agent run workspace code/tests in the Docker sandbox (needs --write-root)')
    parser.add_argument('--team-timeout', type=float, help='Team time budget in seconds (default 180)')
    parser.add_argument("--max-executions", type=int, default=16)
    parser.add_argument("--max-replans", type=int, default=2)
    parser.add_argument('--compress-history', action='store_true',
                        help='Generate archived stage summaries using the existing local compressor')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
