from sqlalchemy.orm import Session

from aiive.db.models import Chunk, Document
from aiive.knowledge.ingestor import search_chunks
from aiive.knowledge.qdrant_indexer import QdrantIndexer


class RetrievalPlanner:
    def __init__(self, db: Session, indexer: QdrantIndexer | None = None):
        self._db = db
        self._indexer = indexer or QdrantIndexer()

    def retrieve(self, query: str, limit: int = 5) -> list[dict]:
        results = []

        # Dense (Qdrant vector)
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
            pass

        # Text (PostgreSQL ILIKE)
        text_results = search_chunks(self._db, query, limit)
        existing = {r["chunk_id"] for r in results}
        for tr in text_results:
            if tr["chunk_id"] not in existing:
                tr["source"] = "text"
                results.append(tr)

        # RRF fusion: interleave dense first then text
        return results[:limit]
