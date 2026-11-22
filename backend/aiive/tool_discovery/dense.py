"""Bounded, process-local document embeddings. No registry mutations do IO."""
from __future__ import annotations

import logging
import math
from _thread import LockType
from array import array
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from aiive.memory.embedding_provider import OpenAIEmbeddingProvider
from aiive.tool_discovery.documents import SearchDocument

logger = logging.getLogger(__name__)

DENSE_TOP_K = 20
MAX_CACHED_VECTORS = 2048
MAX_EMBEDDING_CHARS = 450
RETRY_SECONDS = 30


@dataclass(frozen=True)
class ModelIdentity:
    provider: str
    base_url: str
    model: str
    revision: str
    dimensions: int
    query_prefix: str


def normalized(values: list[float], dimensions: int) -> array[float]:
    norm = math.hypot(*values)
    if len(values) != dimensions or not norm or not math.isfinite(norm):
        raise ValueError("invalid tool discovery embedding")
    return array("f", (value / norm for value in values))


def rank_dense(
    documents: Iterable[SearchDocument], vectors: dict[str, array[float]], query: array[float],
) -> list[str]:
    scores = {
        doc.key: sum(x * y for x, y in zip(query, vectors[doc.discovery_hash], strict=True))
        for doc in documents
    }
    return sorted((key for key, score in scores.items() if score > 0),
                  key=lambda key: (-scores[key], key))[:DENSE_TOP_K]


class DenseIndex:
    def __init__(
        self, identity: ModelIdentity, provider_factory: Callable[[], OpenAIEmbeddingProvider],
        *, batch_size: int, capacity: int = MAX_CACHED_VECTORS,
    ) -> None:
        self.identity: ModelIdentity = identity
        self._provider: Callable[[], OpenAIEmbeddingProvider] = provider_factory
        self._batch_size: int = batch_size
        self._capacity: int = capacity
        self._vectors: OrderedDict[str, array[float]] = OrderedDict()
        self._lock: LockType = Lock()
        self._pool: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-discovery")
        self._job: Future[None] | None = None
        self._retry_at: float = 0.0
        self._closed: bool = False

    def vectors(self, documents: Iterable[SearchDocument]) -> tuple[str, dict[str, array[float]]]:
        """Read only this snapshot's vectors, or schedule its missing batch."""
        required = {doc.discovery_hash: doc.text[:MAX_EMBEDDING_CHARS] for doc in documents}
        if len(required) > self._capacity:
            return "index_capacity_exceeded", {}
        with self._lock:
            if self._closed or monotonic() < self._retry_at:
                return "embedding_unavailable", {}
            selected = {}
            missing = {}
            for fingerprint, text in required.items():
                vector = self._vectors.get(fingerprint)
                if vector is None:
                    missing[fingerprint] = text
                else:
                    self._vectors.move_to_end(fingerprint)
                    selected[fingerprint] = vector
            if not missing:
                return "ready", selected
            if self._job is None or self._job.done():
                self._job = self._pool.submit(self._fill, missing)
            return "index_warming", {}

    def query(self, text: str) -> array[float] | None:
        try:
            return normalized(self._provider().embed(text, input_type="query"), self.identity.dimensions)
        except Exception:
            self._failed()
            return None

    def _fill(self, missing: dict[str, str]) -> None:
        try:
            provider = self._provider()
            entries = list(missing.items())
            for start in range(0, len(entries), self._batch_size):
                with self._lock:
                    if self._closed:
                        return
                window = entries[start:start + self._batch_size]
                vectors = provider.embed_batch([text for _, text in window], batch_size=self._batch_size)
                prepared = {
                    fingerprint: normalized(vector, self.identity.dimensions)
                    for (fingerprint, _), vector in zip(window, vectors, strict=True)
                }
                with self._lock:
                    if self._closed:
                        return
                    self._vectors.update(prepared)
                    while len(self._vectors) > self._capacity:
                        self._vectors.popitem(last=False)
        except Exception:
            self._failed()

    def _failed(self) -> None:
        with self._lock:
            if self._closed:
                return
            if monotonic() >= self._retry_at:
                logger.warning("Tool discovery embeddings unavailable; using lexical retrieval", exc_info=True)
            self._retry_at = monotonic() + RETRY_SECONDS

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._vectors.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
