"""MemoryProjection: memory export/formatting service.

Exports active memory records to Markdown / JSON formats.
Adapted for the canonical schema (canonical_key, scope_type, etc.).
"""
import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord
from aiive.memory.memory_types import LifecycleState
from typing import Any

logger = logging.getLogger(__name__)


class MemoryProjection:
    """Memory projection: export active records in human/ machine-readable formats."""

    def __init__(self, db: Session):
        self._db: Session = db

    def to_markdown(self) -> str:
        """Export active memories as Markdown."""
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value)
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )
        lines = ["# AIive Active Memories", "", f"Generated: {len(records)} records\n"]
        for r in records:
            key_info = f"({r.canonical_key})" if r.canonical_key else ""
            lines.append(
                f"- [{r.memory_type}] {r.content}"
                + f" (confidence: {r.confidence:.2f}, importance: {r.importance:.2f})"
                + f" {key_info}"
            )
        return "\n".join(lines)

    def to_json(self) -> list[dict[str, Any]]:
        """Export active memories as JSON-serializable list."""
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value)
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )
        return [
            {
                "id": r.id,
                "memory_type": r.memory_type,
                "canonical_key": r.canonical_key,
                "scope_type": r.scope_type,
                "scope_id": r.scope_id,
                "content": r.content,
                "confidence": r.confidence,
                "importance": r.importance,
                "lifecycle_state": r.lifecycle_state,
                "validity_state": r.validity_state,
                "record_version": r.record_version,
                "lineage": r.lineage,
            }
            for r in records
        ]

    def write_projection(self, output_dir: Path) -> dict[str, Any]:
        """Write Markdown + JSON projections to directory."""
        output_dir.mkdir(parents=True, exist_ok=True)
        md = self.to_markdown()
        js = self.to_json()

        (output_dir / "memories.md").write_text(md)
        (output_dir / "memories.json").write_text(
            json.dumps(js, indent=2, ensure_ascii=False)
        )

        return {
            "markdown": str(output_dir / "memories.md"),
            "json": str(output_dir / "memories.json"),
        }
