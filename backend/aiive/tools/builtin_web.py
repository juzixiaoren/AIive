"""Domain tool handlers (implementation) split from builtin_tools."""

from aiive.tools.base import BaseTool

class WebSearchTool(BaseTool):
    def _run(self, query: str, limit: int = 5):
        from aiive.web import search_web

        return search_web(query, limit=limit)


class FetchWebPageTool(BaseTool):
    def _run(self, url: str, max_chars: int = 100_000):
        from aiive.web import fetch_web_page

        return fetch_web_page(url, max_chars=max_chars)
