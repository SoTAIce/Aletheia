"""Start with python -m app.tools.web_search.server (stdio transport)."""

import json
import os
from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from app.tools.web_search.web_search_service import WebSearchService


def create_service(api_key: str | None = None) -> FastMCP:
    if api_key is None:
        api_key = os.environ.get("TAVILY_API_KEY", "")
    service = WebSearchService(api_key)
    mcp = FastMCP("AletheiaWebSearch")

    # The adapter includes both text and structured content; emit JSON once.
    @mcp.tool(structured_output=False)
    async def search_web(
        query: str,
        max_results: int = 5,
    ) -> str:
        """Search the web; return JSON title, url and snippet results.

        max_results must be between 1 and 10. Use returned URLs as evidence.
        """
        results = await service.search(query, max_results)
        return json.dumps(
            [asdict(item) for item in results],
            ensure_ascii=False,
        )

    return mcp


def main() -> None:
    create_service().run(transport="stdio")


if __name__ == "__main__":
    main()
