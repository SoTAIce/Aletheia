"""Run the web backend from the project root: python -m app.api (D-046).

Writing files and the sandbox are made available at startup (--write-root, --sandbox); each
message may then turn writing off, pick its mode and skip long-term memory (D-051). A token is required unless WEB_ALLOW_NO_TOKEN=true on a loopback host.
"""

import argparse
import asyncio
import ipaddress
from contextlib import AsyncExitStack

from app.agent.__main__ import sandbox_problem, step_timeout_for, team_options_for
from app.config import config
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_loader import local_mcp_servers


def is_loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def startup_problem(host: str, token: str, allow_no_token: bool) -> str | None:
    """Reason the server must not start; None when fine. Never echoes the token."""
    if token:
        return None
    if not allow_no_token:
        return 'WEB_ACCESS_TOKEN is empty; set it, or WEB_ALLOW_NO_TOKEN=true for loopback-only testing'
    if not is_loopback(host):
        return 'WEB_ALLOW_NO_TOKEN=true is only accepted on a loopback WEB_HOST'
    return None


async def serve(args) -> int:
    import uvicorn
    from app.api.app import create_app

    host, port = args.host or config.web_host, args.port or config.web_port
    problem = startup_problem(host, config.web_access_token, config.web_allow_no_token)
    if problem:
        print(f'WebStartupError: {problem}')
        return 2
    write_root = args.write_root or config.mcp_filesystem_write_root or None
    problem = sandbox_problem(write_root, args.sandbox)
    if problem:
        print(f'SandboxUnavailableError: {problem}')
        return 2
    filesystem_root = args.filesystem_root or config.mcp_filesystem_root or None
    # Every directory passes the same checks; one on the system drive needs confirming (D-074).
    from app.services.workspace import WorkspacePolicy, confirm_startup
    from app.tools.tool_loader import workspace_servers
    from app.services.folder_picker import FolderPicker
    policy = WorkspacePolicy.from_config()
    problem = confirm_startup(policy, (filesystem_root, write_root), confirmed=args.confirm_system_drive)
    if problem:
        print(f'WorkspaceError: {problem}')
        return 2
    # The page may choose the working directory on a loopback host; elsewhere only inside
    # WORKSPACE_ALLOWED_ROOTS, since anyone who reaches the page could point the agent anywhere.
    choice = config.workspace_choice and (is_loopback(host) or bool(policy.allowed_roots))
    if config.workspace_choice and not choice:
        print('工作目录选择已关闭：非本机地址需要先配置 WORKSPACE_ALLOWED_ROOTS。', flush=True)
    async with AsyncExitStack() as stack:
        memory = None
        if config.longterm_memory_dsn:
            from app.services.longterm_memory_setup import open_longterm_memory
            memory = await stack.enter_async_context(open_longterm_memory())
        # The team is always prepared under the fixed-team rules so a message may pick it; the default
        # mode stays --execution-mode, and auto still allows the team only with writes and the sandbox
        # (D-026). The step timeout widens for team tasks only, inside the service (D-051).
        team_options = team_options_for('team', write_root or choice, args.sandbox)
        service = await stack.enter_async_context(RagAgentService(
            mcp_servers=local_mcp_servers(
                filesystem_root=filesystem_root,
                write_root=write_root, tavily_api_key=config.tavily_api_key, sandbox=args.sandbox,
                deny=policy.closed_folders()),
            # A chosen directory is also the write directory when a message turns writing on.
            workspace_servers=(workspace_servers(tavily_api_key=config.tavily_api_key, sandbox=args.sandbox,
                                                 writes=True, deny=policy.closed_folders())
                               if choice else None),
            workspace_policy=policy, default_workspace=write_root or filesystem_root,
            include_knowledge=not args.no_knowledge, execution_mode=args.execution_mode,
            single_runner=args.single_runner, delegate=args.delegate,
            team_options=team_options, step_timeout_seconds=step_timeout_for(args.sandbox, None),
            longterm_memory=memory, longterm_recall_limit=config.longterm_recall_limit,
        ))
        app = create_app(service, user_id=config.web_user_id, access_token=config.web_access_token,
                         memory=memory, write_enabled=bool(write_root) or choice,
                         write_default=bool(write_root),
                         max_concurrent_runs=config.web_max_concurrent_runs,
                         event_retention_seconds=config.web_event_retention_seconds,
                         shutdown_timeout_seconds=config.web_shutdown_timeout_seconds,
                         approval_tools=config.web_approval_tool_names,
                         approval_wait_seconds=config.web_approval_timeout_seconds,
                         # The system folder window opens on the server's screen, so only for loopback.
                         folder_picker=FolderPicker() if choice and is_loopback(host) else None)
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None,
                                               access_log=False, server_header=False))
        print(f'Aletheia 网页后端：http://{host}:{port}（写文件：{"开启" if write_root else "关闭"}，'
              f'执行方式：单一循环）', flush=True)
        await server.serve()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description='Aletheia web backend (local, single user)')
    parser.add_argument('--host', help='Listen address (default WEB_HOST)')
    parser.add_argument('--port', type=int, help='Listen port (default WEB_PORT)')
    parser.add_argument('--filesystem-root', help='Directory exposed by the filesystem MCP server')
    parser.add_argument('--write-root', help='Optional writable directory; omitted means read-only')
    parser.add_argument('--sandbox', action='store_true', help='Allow the Docker sandbox (needs --write-root)')
    parser.add_argument('--confirm-system-drive', action='store_true',
                        help='Confirm a --filesystem-root/--write-root on the system drive (C:) without a prompt (D-074)')
    parser.add_argument('--no-knowledge', action='store_true', help='Disable RAG when no knowledge store is configured')
    parser.add_argument('--execution-mode', choices=['single', 'team', 'auto'], default='single')
    parser.add_argument('--single-runner', choices=['loop'], default='loop',
                        help='one agent loop; plan mode was removed (D-076)')
    parser.add_argument('--delegate', action='store_true',
                        help='Let the single loop delegate wide read-only research (off by default)')
    return parser


if __name__ == '__main__':
    from app.core.logging_setup import configure_logging
    configure_logging()
    try:
        raise SystemExit(asyncio.run(serve(build_parser().parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
