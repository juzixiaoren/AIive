"""Bounded process-local LRU for immutable parsed attachment documents.

Authorization always runs outside and before this cache. No bytes or index persist.
The lock also makes misses single-flight and bounds concurrent parsing work.
"""

from __future__ import annotations

import sys
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import is_dataclass
from threading import RLock
from typing import Any, final

from aiive.knowledge import blocks, chunker
from aiive.knowledge.chunker import ChunkDraft, ChunkConfig
from aiive.runtime.attachments import BUCKET
from aiive.storage import object_store

MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 16


def _size(value: Any, seen: set[int] | None = None) -> int:
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(_size(k, seen) + _size(v, seen) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return size + sum(_size(v, seen) for v in value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + _size(vars(value), seen)
    return size


@final
class DocumentParseCache:
    def __init__(
        self,
        max_bytes: int = MAX_CACHE_BYTES,
        max_entry_bytes: int = MAX_ENTRY_BYTES,
        max_entries: int = MAX_ENTRIES,
    ):
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.max_entries = max_entries
        self._lock = RLock()
        self._entries: OrderedDict[
            tuple[str, ...], tuple[tuple[ChunkDraft, ...], int]
        ] = OrderedDict()
        self.bytes_used = 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.bytes_used = 0

    def get(
        self, key: tuple[str, ...], loader: Callable[[], tuple[ChunkDraft, ...]]
    ) -> tuple[ChunkDraft, ...]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry[0]
            result = loader()
            # Account for all reachable Python objects plus conservative LRU overhead.
            weight = _size((key, result)) + 512
            if (
                weight > min(self.max_entry_bytes, self.max_bytes)
                or self.max_entries < 1
            ):
                return result
            while self._entries and (
                self.bytes_used + weight > self.max_bytes
                or len(self._entries) >= self.max_entries
            ):
                _, (_, removed) = self._entries.popitem(last=False)
                self.bytes_used -= removed
            self._entries[key] = (result, weight)
            self.bytes_used += weight
            return result


DOCUMENT_CACHE = DocumentParseCache()


def document_chunks(
    attachment_id: str, sha256: str, object_key: str, filename: str
) -> tuple[ChunkDraft, ...]:
    key = (
        attachment_id,
        sha256,
        blocks.PARSER_VERSION,
        chunker.CHUNKER_VERSION,
        object_key,
        filename,
        str(object_store.ROOT),
        "350:450",
    )

    def load() -> tuple[ChunkDraft, ...]:
        try:
            data = object_store.get_verified(
                object_store.ObjectRef(BUCKET, object_key), sha256
            )
        except (OSError, ValueError) as exc:
            raise ValueError("attachment_bytes_unavailable") from exc
        return tuple(
            chunker.chunk_blocks(
                blocks.parse_document_blocks(data, filename),
                ChunkConfig(target_chunk_tokens=350, max_chunk_tokens=450),
            )
        )

    return DOCUMENT_CACHE.get(key, load)
