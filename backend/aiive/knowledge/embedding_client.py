import hashlib
from abc import ABC, abstractmethod
from typing import Sequence

EMBEDDING_DIM = 384


class EmbeddingClient(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class FakeEmbeddingClient(EmbeddingClient):
    """Deterministic pseudo-embedding for unit tests only. NOT for production.
    Uses SHA-256 hashing - semantically different texts will have completely different vectors.
    Production MUST use a real embedding service (e.g., OpenAI text-embedding-3-small).
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            vec = []
            for i in range(EMBEDDING_DIM):
                byte_idx = i % len(h)
                val = (h[byte_idx] / 255.0) * 2 - 1
                # Add slight variation per dimension
                val += (i * 0.0001)
                vec.append(round(val, 6))
            results.append(vec)
        return results
