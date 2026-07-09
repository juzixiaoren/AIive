import uuid
from dataclasses import dataclass, field
from typing import Optional

from aiive.knowledge.embedding_client import EMBEDDING_DIM, EmbeddingClient, FakeEmbeddingClient


@dataclass
class QdrantPoint:
    id: str
    vector: list[float]
    payload: dict = field(default_factory=dict)


class QdrantClient:
    """Qdrant-compatible client. Uses in-memory storage for local dev. 
For production: pip install qdrant-client and replace with real QdrantClient.
"""

    def __init__(self, host: str = "localhost", port: int = 6333):
        self._host = host
        self._port = port
        self._collections: dict[str, dict[str, QdrantPoint]] = {}

    def create_collection(self, name: str, vector_size: int) -> None:
        self._collections[name] = {}

    def delete_collection(self, name: str) -> None:
        self._collections.pop(name, None)

    def upsert(self, collection: str, points: list[QdrantPoint]) -> None:
        if collection not in self._collections:
            self._collections[collection] = {}
        for p in points:
            self._collections[collection][p.id] = p

    def search(
        self, collection: str, query_vector: list[float], limit: int = 5
    ) -> list[QdrantPoint]:
        if collection not in self._collections:
            return []
        results = []
        for point in self._collections[collection].values():
            score = _cosine_similarity(query_vector, point.vector)
            results.append((score, point))
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class QdrantIndexer:
    def __init__(
        self,
        qdrant: QdrantClient | None = None,
        embedding: EmbeddingClient | None = None,
    ):
        self._qdrant = qdrant or QdrantClient()
        self._embedding = embedding or FakeEmbeddingClient()
        self._collection = "kb_docs"

    def ensure_collection(self) -> None:
        self._qdrant.create_collection(self._collection, EMBEDDING_DIM)

    def index_chunks(self, chunks: list[dict]) -> int:
        texts = [c["content"] for c in chunks]
        vectors = self._embedding.embed(texts)

        points = []
        for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
            points.append(QdrantPoint(
                id=chunk.get("chunk_id", chunk.get("id", str(uuid.uuid4()))),
                vector=vec,
                payload={
                    "chunk_id": chunk.get("chunk_id", chunk.get("id", "")),
                    "document_id": chunk.get("document_id", ""),
                    "source_path": chunk.get("source_path", ""),
                },
            ))

        self._qdrant.upsert(self._collection, points)
        return len(points)

    def search(self, query: str, limit: int = 5) -> list[QdrantPoint]:
        query_vec = self._embedding.embed([query])[0]
        return self._qdrant.search(self._collection, query_vec, limit)

    def rebuild_from_db(self, chunks: list[dict]) -> None:
        self.ensure_collection()
        self.index_chunks(chunks)

    def cleanup(self) -> None:
        self._qdrant.delete_collection(self._collection)
