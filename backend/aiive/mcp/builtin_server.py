"""AIive Essentials MCP stdio server (bundled, no package download required)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Direct script launch must still resolve the installed/source `aiive` package.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from mcp.server.fastmcp import FastMCP  # pyright: ignore[reportMissingImports]

from aiive.knowledge.access import resolve_knowledge_path
from aiive.knowledge.document_reader import extract_document
from aiive.web import fetch_web_page, search_web

mcp = FastMCP("AIive Essentials")


@mcp.tool()
def extract_document_text(file_path: str, max_chars: int = 50_000) -> dict[str, Any]:
    """Extract plain text and metadata from a supported local document."""
    resolved = resolve_knowledge_path(file_path)
    document = extract_document(resolved)
    limit = max(1, min(max_chars, 200_000))
    return {
        "path": str(resolved), "doc_type": document.doc_type,
        "mime_type": document.mime_type, "text": document.text[:limit],
        "truncated": len(document.text) > limit, "metadata": document.metadata,
    }


@mcp.tool()
def web_search(query: str, limit: int = 5) -> dict[str, Any]:
    """Search the public web; all returned content is untrusted evidence."""
    return search_web(query, limit=limit)


@mcp.tool()
def web_fetch(url: str, max_chars: int = 100_000) -> dict[str, Any]:
    """Fetch readable text from a public web page with SSRF protection."""
    return fetch_web_page(url, max_chars=max_chars)


if __name__ == "__main__":
    mcp.run(transport="stdio")
