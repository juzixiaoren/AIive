import json
from pathlib import Path

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord


class MemoryProjection:
    def __init__(self, db: Session):
        self._db = db

    def to_markdown(self) -> str:
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == "active")
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )
        lines = ["# AIive Active Memories", "", f"Generated: {len(records)} records\n"]
        for r in records:
            lines.append(f"- [{r.memory_type}] {r.content} (confidence: {r.confidence:.2f})")
        return "\n".join(lines)

    def to_json(self) -> list[dict]:
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == "active")
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )
        return [
            {
                "id": r.id,
                "memory_type": r.memory_type,
                "content": r.content,
                "confidence": r.confidence,
                "lineage": r.lineage,
            }
            for r in records
        ]

    def write_projection(self, output_dir: Path) -> dict:
        output_dir.mkdir(parents=True, exist_ok=True)
        md = self.to_markdown()
        js = self.to_json()

        (output_dir / "memories.md").write_text(md)
        (output_dir / "memories.json").write_text(json.dumps(js, indent=2, ensure_ascii=False))

        return {"markdown": str(output_dir / "memories.md"), "json": str(output_dir / "memories.json")}
