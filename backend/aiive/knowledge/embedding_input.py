"""Chunk 原文与 Embedding 输入的分离。

* **Chunk 原文**（`chunks.content`）用于展示、关键词检索与回源校验，保持文档原貌；
* **Embedding 输入**额外拼接文档标题与章节路径，让「AOF」这类短正文片段在向量
  空间里带上主题上下文，避免「通过记录写命令实现持久化」被召回时丢失所属主题。

两者必须显式区分：
- 向量哈希覆盖**完整 Embedding 输入**（含标题前缀），不能只哈希正文，否则标题
  变化（例如文档改名、章节调整）不会触发重新 Embedding；
- 展示与关键词检索仍使用正文，不会把「文档：X / 章节：Y / 正文：」前缀塞进给用户
  的内容里。

模板版本变化（例如调整前缀措辞）会让所有向量失效：`EMBEDDING_INPUT_VERSION`
参与 `KnowledgeChunkVector` 的新鲜度判定。
"""
from __future__ import annotations

import hashlib

EMBEDDING_INPUT_VERSION = "title_context_v1"

_TITLE_PREFIX = "文档："
_HEADING_PREFIX = "章节："
_CONTENT_PREFIX = "正文："


def build_embedding_input(
    *,
    title: str | None,
    heading_path: tuple[str, ...],
    content: str,
) -> str:
    """构造 Embedding 输入：标题上下文 + 正文。"""
    lines: list[str] = []
    if title and title.strip():
        lines.append(f"{_TITLE_PREFIX}{title.strip()}")
    if heading_path:
        lines.append(f"{_HEADING_PREFIX}{' > '.join(heading_path)}")
    lines.append(f"{_CONTENT_PREFIX}{content}")
    return "\n".join(lines)


def embedding_input_hash(embedding_input: str) -> str:
    """返回 Embedding 输入的稳定哈希（覆盖标题前缀与正文）。"""
    return hashlib.sha256(embedding_input.encode("utf-8")).hexdigest()
