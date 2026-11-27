"""Stable, shared task-family and lexical matching rules."""
from __future__ import annotations

from typing import Any

import re
from uuid import NAMESPACE_URL, uuid5

from aiive.tool_discovery.documents import tokens

_FAMILIES = (
    ("selfdev", r"\bselfdev\b|修复代码|修复测试"),
    ("mcp", r"\bmcp\b"),
    ("coding", r"\b(?:code|coding|pytest|refactor|compile|frontend|backend)\b|代码|编译|前端|后端|重构"),
    ("document", r"\b(?:pdf|docx|document|spreadsheet|xlsx)\b|文档|表格"),
    ("knowledge", r"\bknowledge base\b|知识库"),
    ("web_research", r"\b(?:web search|research|look up|browse)\b|调研|联网|搜索|查找"),
)
_TYPE_ALIASES = {"research": "web_research", "code": "coding", "self_improvement": "selfdev"}


def family_for(text: str, task_type: str = "general") -> str:
    if task_type not in {"general", "chat", ""}:
        return _TYPE_ALIASES.get(task_type, task_type)[:64]
    return next((family for family, pattern in _FAMILIES if re.search(pattern, text, re.I)), "general")


def match_terms(text: str) -> set[str]:
    return set(tokens(text[:4000])) - {
        "the", "and", "for", "with", "a", "to", "in", "of", "is",
        "请", "一下", "帮我", "这个", "需要", "一个", "可以",
    }


def stable_id(*parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "aiive:experience:" + ":".join(parts)))


def merge_uses(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({(item["id"], item["version"]): item for group in groups for item in group}.values())
