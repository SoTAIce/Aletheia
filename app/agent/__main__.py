"""Run from the project root: python -m app.agent \"Your goal\"."""

import argparse
import asyncio
from contextlib import AsyncExitStack

from app.config import config
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_loader import local_mcp_servers


def team_options_for(execution_mode, write_root):
    """Team stays read-only unless a write root is configured; then workers may write files."""
    if execution_mode != 'team' or not write_root:
        return None
    from app.agent.team.models import TeamOptions
    defaults = TeamOptions()
    return TeamOptions(allowed_tools=defaults.allowed_tools | {'write_file'})


async def main(args) -> int:
    async with AsyncExitStack() as stack:
        compressor = None
        if getattr(args, 'compress_history', False):
            if not config.conversation_storage_dir:
                raise ValueError('--compress-history requires CONVERSATION_STORAGE_DIR')
            from app.core.dependencies import open_conversation_compressor
            compressor = await stack.enter_async_context(open_conversation_compressor())
        write_root = args.write_root or config.mcp_filesystem_write_root or None
        service = await stack.enter_async_context(RagAgentService(
            mcp_servers=local_mcp_servers(
                filesystem_root=args.filesystem_root or config.mcp_filesystem_root or None,
                write_root=write_root,
                tavily_api_key=config.tavily_api_key,
            ),
            include_knowledge=not args.no_knowledge,
            max_executions=args.max_executions, max_replans=args.max_replans,
            conversation_compressor=compressor,
            execution_mode=getattr(args, 'execution_mode', 'single'),
            team_options=team_options_for(getattr(args, 'execution_mode', 'single'), write_root),
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
    parser.add_argument("--max-executions", type=int, default=16)
    parser.add_argument("--max-replans", type=int, default=2)
    parser.add_argument('--compress-history', action='store_true',
                        help='Generate archived stage summaries using the existing local compressor')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
