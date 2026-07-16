"""Phase 5：冷热历史分层与统一检索包。

入口为 UnifiedRetriever；索引读写由 RetrievalIndexManager 负责；
索引刷新 / 全量重建分别由 indexing_service / index_rebuild 提供，
供 Outbox handler 调用。
"""
from __future__ import annotations

from aiive.retrieval.retrieval_types import (
    RetrievalHit,
    RetrievalMode,
    RetrievalRequest,
    RetrievalResult,
    RetrievalSourceType,
)
from aiive.retrieval.unified_retriever import UnifiedRetriever

__all__ = [
    "RetrievalHit",
    "RetrievalMode",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalSourceType",
    "UnifiedRetriever",
]
