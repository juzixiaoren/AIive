"""Phase 5：真实 source version 提取（禁止只依赖 updated_at）。

source version 仅做精确 equality 比较，不做跨类型字符串大小比较。
EpochCheckpoint.source_hashes 先 canonical JSON 再 SHA-256。
"""
from __future__ import annotations

import hashlib
import json

from aiive.db.models import EpochCheckpoint, MemoryRecord, SegmentSummary


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def memory_source_version(r: MemoryRecord) -> tuple[str, str]:
    """返回 (source_version, source_hash)。"""
    version = str(r.record_version)
    h = r.content_hash or _sha256_hex(r.content or "")
    return version, h


def segment_summary_source_version(s: SegmentSummary) -> tuple[str, str]:
    version = str(s.summary_version)
    h = s.source_hash or ""
    return version, h


def epoch_checkpoint_source_version(c: EpochCheckpoint) -> tuple[str, str]:
    version = str(c.version)
    h = epoch_checkpoint_source_hashes_hash(c)
    return version, h


def epoch_checkpoint_source_hashes_hash(c: EpochCheckpoint) -> str:
    """source_hashes 先 canonical JSON 再 SHA-256（精确 equality）。"""
    normalized = json.dumps((c.source_hashes or []), sort_keys=True, ensure_ascii=False)
    return _sha256_hex(normalized)


def version_cmp(a: str, b: str) -> int:
    """同类型 source_version 比较：可转 int 则按 int，否则按字符串。

    仅用于同 source 的 fencing，不做跨类型语义比较。
    """
    try:
        ia, ib = int(a), int(b)
        return (ia > ib) - (ia < ib)
    except (ValueError, TypeError):
        return (a > b) - (a < b)
