"""Bounded text previews for the Context Inspector."""

import re

_PREVIEW_CHARS = 800
_PREVIEW_LINES = 4
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


def context_content_preview(content: str) -> str:
    """Keep the first four logical lines and at most 800 original characters."""
    # Include one extra character so a CRLF at the boundary is matched as a unit.
    for line, match in enumerate(_LINE_BREAK.finditer(content, 0, _PREVIEW_CHARS + 1), 1):
        if line == _PREVIEW_LINES and match.end() < len(content):
            return content[:match.start()]

    end = _PREVIEW_CHARS
    if content[end - 1:end + 1] == "\r\n":
        end -= 1
    return content[:end]
