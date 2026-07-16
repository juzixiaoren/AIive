"""Phase 5：索引 token 化（复用 automatic_recall 的 CJK 感知分词）。

生成中文二元 token 与英文规范 token，供可移植倒排表 retrieval_index_tokens 使用。
"""
from __future__ import annotations

import re

from aiive.memory.automatic_recall import tokenize_cjk_aware

_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def tokenize_for_index(text: str) -> list[tuple[str, str]]:
    """返回 (token, token_kind) 列表，去重。

    token_kind ∈ {cjk_bigram, word}：CJK 二元走 cjk_bigram，英文/数字走 word。
    """
    if not text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tok in tokenize_cjk_aware(text.lower()):
        kind = "cjk_bigram" if _CJK.search(tok) else "word"
        key = (tok, kind)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out
