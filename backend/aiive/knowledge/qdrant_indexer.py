"""
Qdrant 向量索引模块。

提供兼容 Qdrant 接口的内存向量存储和索引器。
本地开发使用内存存储，生产环境需安装 qdrant-client 并替换为真实 QdrantClient。
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from aiive.knowledge.embedding_client import EMBEDDING_DIM, EmbeddingClient, FakeEmbeddingClient

logger = logging.getLogger(__name__)


@dataclass
class QdrantPoint:
    """Qdrant 向量点，包含唯一标识、向量和负载数据。"""
    id: str
    vector: list[float]
    payload: dict = field(default_factory=dict)


class QdrantClient:
    """
    兼容 Qdrant 接口的客户端。

    本地开发使用内存存储，无需外部依赖。
    生产环境：pip install qdrant-client 并替换为真实 QdrantClient。
    """

    def __init__(self, host: str = "localhost", port: int = 6333):
        self._host = host
        self._port = port
        self._collections: dict[str, dict[str, QdrantPoint]] = {}  # 内存中的集合存储

    def create_collection(self, name: str, vector_size: int) -> None:
        """创建指定名称和向量维度的集合。"""
        self._collections[name] = {}

    def delete_collection(self, name: str) -> None:
        """删除指定集合。"""
        self._collections.pop(name, None)

    def upsert(self, collection: str, points: list[QdrantPoint]) -> None:
        """插入或更新向量点，以 id 为主键覆盖。"""
        if collection not in self._collections:
            self._collections[collection] = {}
        for p in points:
            self._collections[collection][p.id] = p

    def search(
        self, collection: str, query_vector: list[float], limit: int = 5
    ) -> list[QdrantPoint]:
        """
        在指定集合中搜索与查询向量最相似的 top-N 点。

        参数:
            collection: 集合名称。
            query_vector: 查询向量。
            limit: 返回结果数量上限。

        返回:
            按余弦相似度降序排列的 QdrantPoint 列表。
        """
        if collection not in self._collections:
            return []
        results = []
        for point in self._collections[collection].values():
            score = _cosine_similarity(query_vector, point.vector)
            results.append((score, point))
        # 按相似度降序排列
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    计算两个向量的余弦相似度。

    参数:
        a, b: 两个等长向量。

    返回:
        [-1, 1] 范围的余弦相似度值。
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class QdrantIndexer:
    """Qdrant 索引器，将文本块向量化并索引到向量数据库。"""

    def __init__(
        self,
        qdrant: QdrantClient | None = None,
        embedding: EmbeddingClient | None = None,
    ):
        """
        初始化索引器。

        参数:
            qdrant: Qdrant 客户端实例，默认使用内存版。
            embedding: 嵌入客户端实例，默认使用伪实现。
        """
        self._qdrant = qdrant or QdrantClient()
        self._embedding = embedding or FakeEmbeddingClient()
        self._collection = "kb_docs"  # 默认知识库集合名

    def ensure_collection(self) -> None:
        """确保目标集合已创建。"""
        self._qdrant.create_collection(self._collection, EMBEDDING_DIM)

    def index_chunks(self, chunks: list[dict]) -> int:
        """
        将文本块列表向量化后写入 Qdrant。

        参数:
            chunks: 块列表，每项需包含 content 字段。

        返回:
            成功索引的块数量。
        """
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
        """
        向量相似度检索。

        参数:
            query: 查询文本。
            limit: 返回结果数量上限。

        返回:
            相似度最高的 QdrantPoint 列表。
        """
        query_vec = self._embedding.embed([query])[0]
        return self._qdrant.search(self._collection, query_vec, limit)

    def rebuild_from_db(self, chunks: list[dict]) -> None:
        """从数据库块数据重建向量索引。"""
        self.ensure_collection()
        self.index_chunks(chunks)

    def cleanup(self) -> None:
        """清理索引，删除集合。"""
        self._qdrant.delete_collection(self._collection)
