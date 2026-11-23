"""Tiny stdio MCP server for tests: checks whether an env var is present (never returns its value)."""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("EnvCheck")


@mcp.tool()
def has_env(name: str) -> str:
    """Return 'true' if the named environment variable is set and non-empty, else 'false'."""
    return "true" if bool(os.environ.get(name)) else "false"


if __name__ == "__main__":
    mcp.run(transport="stdio")
