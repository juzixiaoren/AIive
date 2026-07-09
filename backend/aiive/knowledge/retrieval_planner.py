"""
检索规划模块。

实现混合检索策略：结合向量检索（稠密检索）和关键词检索（文本检索），
通过简单融合（稠密优先、去重追加）返回统一结果集。
"""
import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from aiive.db.models import Chunk, Document
from aiive.knowledge.ingestor import search_chunks
from aiive.knowledge.qdrant_indexer import QdrantIndexer


class RetrievalPlanner:
    """检索规划器，协调稠密检索和文本检索，融合返回结果。"""

    def __init__(self, db: Session, indexer: QdrantIndexer | None = None):
        """
        初始化检索规划器。

        参数:
            db: SQLAlchemy 数据库会话。
            indexer: Qdrant 索引器实例，默认使用内存版。
        """
        self._db = db
        self._indexer = indexer or QdrantIndexer()

    def retrieve(self, query: str, limit: int = 5) -> list[dict]:
        """
        执行混合检索：稠密向量检索 + 文本关键词检索，去重后融合返回。

        参数:
            query: 查询文本。
            limit: 返回结果数量上限，默认 5。

        返回:
            去重后的检索结果列表，每项标注来源（dense 或 text）。
        """
        results = []

        # 第一路：稠密检索（Qdrant 向量相似度）
        try:
            dense = self._indexer.search(query, limit)
            for point in dense:
                chunk = self._db.get(Chunk, point.payload.get("chunk_id", ""))
                if chunk:
                    results.append({
                        "chunk_id": chunk.id,
                        "document_id": chunk.document_id,
                        "content_preview": chunk.content[:200],
                        "source": "dense",
                    })
        except Exception:
            logger.warning("稠密检索失败，回退为文本检索: query=%s", query, exc_info=True)

        # 第二路：文本检索（PostgreSQL ILIKE 关键词匹配）
        text_results = search_chunks(self._db, query, limit)
        existing = {r["chunk_id"] for r in results}  # 已存在的 chunk_id 集合，用于去重
        for tr in text_results:
            if tr["chunk_id"] not in existing:
                tr["source"] = "text"
                results.append(tr)

        # 简单融合：稠密优先，文本补充，截断到 limit
        return results[:limit]
