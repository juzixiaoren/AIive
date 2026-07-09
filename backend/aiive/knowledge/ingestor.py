import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from aiive.db.models import Document, Chunk
from aiive.knowledge.chunker import chunk_text


class KnowledgeIngestor:
    def __init__(self, db: Session):
        self._db = db

    def ingest(self, file_path: str) -> dict:
        path = Path(file_path)
        if not path.exists():
            return {"ok": False, "error": "File not found", "path": file_path}

        content = path.read_text(encoding="utf-8", errors="replace")
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Dedup by hash
        existing = (
            self._db.query(Document)
            .filter(Document.content_hash == content_hash)
            .first()
        )
        if existing:
            chunks = (
                self._db.query(Chunk)
                .filter(Chunk.document_id == existing.id)
                .order_by(Chunk.chunk_index)
                .all()
            )
            return {
                "ok": True, "duplicate": True, "document_id": existing.id,
                "chunks": len(chunks),
            }

        # Determine type
        suffix = path.suffix.lower()
        if suffix in (".md", ".markdown"):
            doc_type = "markdown"
        elif suffix in (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs"):
            doc_type = "code"
        else:
            doc_type = "text"

        doc = Document(
            source_path=str(path.resolve()),
            content_hash=content_hash,
            title=path.name,
            doc_type=doc_type,
        )
        self._db.add(doc)
        self._db.flush()

        # Chunk
        chunks_raw = chunk_text(content)
        chunk_ids = []
        for c in chunks_raw:
            chunk = Chunk(
                document_id=doc.id,
                content=c["content"],
                line_start=c["line_start"],
                line_end=c["line_end"],
                chunk_index=c["chunk_index"],
                token_estimate=max(1, len(c["content"]) // 4),
            )
            self._db.add(chunk)
            chunk_ids.append(chunk.id)

        self._db.flush()
        return {"ok": True, "document_id": doc.id, "chunks": len(chunk_ids)}


def search_chunks(db: Session, query: str, limit: int = 5) -> list[dict]:
    results = (
        db.query(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .filter(Chunk.content.ilike(f"%{query}%"))
        .order_by(Chunk.chunk_index)
        .limit(limit)
        .all()
    )
    return [
        {
            "chunk_id": c.id,
            "document_id": d.id,
            "source_path": d.source_path,
            "content_preview": c.content[:200],
            "line_start": c.line_start,
            "line_end": c.line_end,
        }
        for c, d in results
    ]
