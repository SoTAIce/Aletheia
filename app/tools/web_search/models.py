from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str

WebSerachResult = WebSearchResult  # Compatibility with the original spelling.
