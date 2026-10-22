"""Domain tool handlers (implementation) split from builtin_tools."""

def handle_web_search(query: str, limit: int = 5):
    from aiive.web import search_web

    return search_web(query, limit=limit)

def handle_fetch_web_page(url: str, max_chars: int = 100_000):
    from aiive.web import fetch_web_page

    return fetch_web_page(url, max_chars=max_chars)
