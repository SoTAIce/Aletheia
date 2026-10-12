"""Run from the project root: python -m app.agent \"Your goal\"."""

import argparse
import asyncio

from app.config import config
from app.models.taskstate import TaskStatus
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_loader import local_mcp_servers


async def main(args) -> int:
    service = RagAgentService(
        mcp_servers=local_mcp_servers(
            filesystem_root=args.filesystem_root or config.mcp_filesystem_root or None,
            write_root=args.write_root or config.mcp_filesystem_write_root or None,
            tavily_api_key=config.tavily_api_key,
        ),
        include_knowledge=not args.no_knowledge,
        max_executions=args.max_executions, max_replans=args.max_replans,
    )
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
    parser.add_argument("--max-executions", type=int, default=16)
    parser.add_argument("--max-replans", type=int, default=2)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
