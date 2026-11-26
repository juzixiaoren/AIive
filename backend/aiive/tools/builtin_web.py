"""Domain tool handlers (implementation) split from builtin_tools."""

import json
from typing import override

from aiive.tools.base import BaseTool

class WebSearchTool(BaseTool):
    @override
    def _run(self, query: str, limit: int = 5):
        from aiive.web import search_web

        return search_web(query, limit=limit)


class FetchWebPageTool(BaseTool):
    @override
    def _run(
        self, url: str, max_chars: int = 12_000, offset: int = 0,
        expected_content_hash: str | None = None,
    ):
        from aiive.config import settings
        from aiive.runtime.token_counter import LiteLLMTokenCounter
        from aiive.runtime.tool_normalizer import SINGLE_TOOL_RESULT_INLINE_LIMIT
        from aiive.web import fetch_web_page

        page = fetch_web_page(url, offset=offset, max_chars=max_chars, expected_content_hash=expected_content_hash)
        body = page["text"]
        # Reserve room for the execution receipt and message metadata. Measure the
        # serialized page, since quoted/control characters can expand on the wire.
        while body and LiteLLMTokenCounter.count_text(
            json.dumps(page, ensure_ascii=False), model=settings.aiive_llm_model,
        ) > SINGLE_TOOL_RESULT_INLINE_LIMIT - 200:
            if len(body) == 1:
                raise ValueError("web_page_metadata_exceeds_tool_budget")
            body = body[:max(1, len(body) // 2)]
            page.update(text=body, next_offset=offset + len(body), truncated=True)
        return page
