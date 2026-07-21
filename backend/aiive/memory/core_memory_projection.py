"""Core Memory projection: small, stable, rebuildable projection of memory_records.

memory_records remains the single source of truth for facts and lifecycle.
Core Memory Blocks are a derived projection — only keys explicitly declaring a
`core_memory_role` in MemoryKeyRegistry may participate.

- `build_blocks()`: pure read, computes the blocks live from memory_records.
- `CoreMemoryProjection.refresh()`: persists blocks to `core_memory_blocks`
  (called by the Outbox projection job, never in the hot read path).
- `load_core_memory()`: read path — table if populated, else live rebuild
  (so a lost projection is always reconstructable from memory_records).
"""
from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from aiive.db.models import CoreMemoryBlock as CoreMemoryBlockRow
from aiive.db.models import MemoryRecord
from aiive.memory.memory_key_registry import get_memory_key_registry
from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
from aiive.memory.memory_types import LifecycleState, ValidityState
from aiive.memory.recall_config import RecallConfig
from aiive.memory.recall_models import CoreMemoryBlock


# Priority when trimming to budget (most important first).
_ROLE_PRIORITY: dict[str, int] = {
    "core.human_identity": 0,
    "core.interaction_defaults": 1,
    "core.agent_persona": 2,
}


def _estimate(text: str) -> int:
    return max(1, len(text) // 4)


def build_blocks(db: Session, config: RecallConfig | None = None) -> list[CoreMemoryBlock]:
    """Compute Core Memory blocks live from memory_records (pure read)."""
    config = config or RecallConfig()
    reg = get_memory_key_registry()
    role_keys: dict[str, str] = reg.get_core_memory_keys()  # key -> role

    buckets: dict[str, list[MemoryRecord]] = {}
    for key, role in role_keys.items():
        recs = (
            db.query(MemoryRecord)
            .filter(
                MemoryRecord.canonical_key == key,
                MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
                MemoryRecord.validity_state == ValidityState.VALID.value,
            )
            .order_by(MemoryRecord.updated_at.desc())
            .all()
        )
        if recs:
            buckets.setdefault(role, []).extend(recs)

    policy = MemoryPolicyEngine()
    blocks: list[CoreMemoryBlock] = []
    for role, recs in buckets.items():
        visible = [
            (record, policy.render_content(
                record.content, record.sensitivity, MemoryReadChannel.LLM_CONTEXT,
            ))
            for record in recs
        ]
        visible = [(record, content) for record, content in visible if content is not None]
        if not visible:
            continue
        lines = [f"{record.canonical_key}: {content}" for record, content in visible]
        content = "\n".join(lines)
        blocks.append(CoreMemoryBlock(
            block_name=role,
            content=content,
            source_memory_ids=[record.id for record, _ in visible],
            projection_version=1,
            token_count=_estimate(content),
        ))

    # Stable order, then budget enforcement.
    blocks.sort(key=lambda b: _ROLE_PRIORITY.get(b.block_name, 9))
    return _enforce_budget(blocks, config)


def _enforce_budget(blocks: list[CoreMemoryBlock], config: RecallConfig) -> list[CoreMemoryBlock]:
    """Keep total tokens under budget; drop/trim lowest-priority blocks first."""
    kept: list[CoreMemoryBlock] = []
    total = 0
    for b in blocks:
        if total + b.token_count > config.core_memory_total_token_budget:
            # try to trim content of this block to fit
            remaining = config.core_memory_total_token_budget - total
            if remaining > 20:
                allowed_chars = remaining * 4
                b = b.model_copy(update={
                    "content": b.content[:allowed_chars] + "…",
                    "token_count": _estimate(b.content[:allowed_chars] + "…"),
                })
                kept.append(b)
                total += b.token_count
            # else: drop this (and any lower-priority) block
            continue
        kept.append(b)
        total += b.token_count
    return kept[: config.core_memory_max_blocks]


def load_core_memory(db: Session, config: RecallConfig | None = None) -> list[CoreMemoryBlock]:
    """Read path: load persisted blocks; fall back to live rebuild if empty."""
    rows = db.query(CoreMemoryBlockRow).all()
    if rows:
        blocks = [
            CoreMemoryBlock(
                block_name=r.block_name,
                content=r.content,
                source_memory_ids=list(r.source_memory_ids or []),
                projection_version=r.projection_version,
                token_count=r.token_count,
            )
            for r in rows
        ]
        blocks.sort(key=lambda b: _ROLE_PRIORITY.get(b.block_name, 9))
        return blocks
    # Lost projection → reconstruct from memory_records (requirement #30).
    return build_blocks(db, config)


class CoreMemoryProjection:
    """Persists Core Memory blocks to the projection table (Outbox job)."""

    @staticmethod
    def refresh(db: Session, memory_id: str = "", record_version: int = 0,
                config: RecallConfig | None = None) -> None:
        """Rebuild and upsert Core Memory blocks.

        Stale-version protection: out-of-order projection jobs carrying an older
        record_version are skipped (requirement #21).
        """
        config = config or RecallConfig()
        blocks = build_blocks(db, config)

        existing = {r.block_name: r for r in db.query(CoreMemoryBlockRow).all()}
        seen: set[str] = set()
        for b in blocks:
            seen.add(b.block_name)
            checksum = hashlib.sha256(b.content.encode()).hexdigest()[:16]
            row = existing.get(b.block_name)
            if row is not None:
                # Skip if the source record version is older than the stored one.
                stored_rv = (row.record_versions or {}).get(memory_id, 0)
                if memory_id and stored_rv and record_version and record_version < stored_rv:
                    continue
                row.content = b.content
                row.source_memory_ids = b.source_memory_ids
                row.token_count = b.token_count
                row.checksum = checksum
                row.projection_version = (row.projection_version or 1) + 1
                rv = dict(row.record_versions or {})
                if memory_id:
                    rv[memory_id] = record_version
                row.record_versions = rv
            else:
                db.add(CoreMemoryBlockRow(
                    block_name=b.block_name,
                    content=b.content,
                    source_memory_ids=b.source_memory_ids,
                    projection_version=1,
                    record_versions={memory_id: record_version} if memory_id else {},
                    token_count=b.token_count,
                    checksum=checksum,
                ))
        # Remove blocks that no longer have any source record.
        for name, row in existing.items():
            if name not in seen:
                db.delete(row)
        db.flush()
