"""Sandbox MCP server: python -m app.tools.sandbox.server --workspace <write root>.

Exposes three tools backed by DockerSandbox (sandbox-step3-001; run_python_code D-028). The
workspace is fixed by the host at start-up; models only pick a workspace file.
Results start with "exit code N;" (or "timed out"/"killed") so the host can
check for a passing run without parsing free text.
"""

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.tools.sandbox.executor import DockerSandbox, SandboxError

RUN_TOOLS = frozenset({'run_tests', 'run_python'})
READ_ONLY_RUN_TOOL = 'run_python_code'   # offered only to read-only tasks (D-028)


def run_tests_text(sandbox: DockerSandbox, path: str = '') -> str:
    try:
        return sandbox.run_tests(path or None).summary()
    except SandboxError as exc:
        raise ValueError(str(exc)) from None


def run_python_text(sandbox: DockerSandbox, path: str, args: list[str] | None = None) -> str:
    try:
        return sandbox.run_python(path, args or ()).summary()
    except SandboxError as exc:
        raise ValueError(str(exc)) from None


def run_python_code_text(sandbox: DockerSandbox, code: str) -> str:
    try:
        return sandbox.run_python_code(code).summary()
    except SandboxError as exc:
        raise ValueError(str(exc)) from None


def create_server(workspace: Path, sandbox: DockerSandbox | None = None) -> FastMCP:
    sandbox = sandbox or DockerSandbox(workspace)
    mcp = FastMCP('AletheiaSandbox')

    @mcp.tool()
    def run_tests(path: str = '') -> str:
        """Run pytest in an isolated sandbox on the workspace (the WRITE root).

        path: optional test file or directory relative to the workspace; empty runs all tests.
        Write code and test files with write_file first. The sandbox has no network,
        Python 3.13 with pytest only, 30 seconds, 512MB memory, and sees only the workspace.
        Returns "exit code N; seconds" then pytest output (exit code 0 means all tests passed).
        """
        return run_tests_text(sandbox, path)

    @mcp.tool()
    def run_python(path: str, args: list[str] | None = None) -> str:
        """Run one .py file from the workspace in the same isolated sandbox.

        path: .py file relative to the workspace; args: up to 16 short plain strings.
        Returns "exit code N; seconds" then combined stdout/stderr (truncated when long).
        """
        return run_python_text(sandbox, path, args)

    @mcp.tool()
    def run_python_code(code: str) -> str:
        """Run a short Python snippet in the isolated sandbox to compute or check a value.

        code: Python source, at most 8000 characters; print what you need. The working directory is the
        workspace, mounted READ-ONLY: you can import and call its modules but cannot create or change files
        there (only /tmp is writable). No network, Python 3.13, 30 seconds, 512MB memory.
        Returns "exit code N; seconds" then combined stdout/stderr (truncated when long).
        """
        return run_python_code_text(sandbox, code)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description='Aletheia sandbox MCP server')
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    create_server(Path(args.workspace)).run()


if __name__ == '__main__':
    main()
