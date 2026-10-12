"""Small asynchronous Tavily Search API client."""

import math

import httpx

from app.tools.web_search.models import WebSearchResult


class WebSearchService:
    def __init__(
        self, api_key: str, timeout: float = 10.0, max_result_limit: int = 10
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("TAVILY_API_KEY must be set to a non-empty value")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if type(max_result_limit) is not int or not 1 <= max_result_limit <= 20:
            raise ValueError("max_result_limit must be an integer between 1 and 20")
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._max_result_limit = max_result_limit

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if type(max_results) is not int or not 1 <= max_results <= self._max_result_limit:
            raise ValueError(f"max_results must be between 1 and {self._max_result_limit}")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query.strip(), "max_results": max_results, "search_depth": "basic"},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("Tavily returned an invalid results payload")
        results = []
        for item in payload["results"][:max_results]:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(field), str) for field in ("title", "url", "content")
            ):
                raise ValueError("Tavily returned an invalid search result")
            results.append(WebSearchResult(item["title"], item["url"], item["content"]))
        return results
