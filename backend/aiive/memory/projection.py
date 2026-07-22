"""记忆文件投影服务：从 PostgreSQL 真相源生成 Markdown 和 JSON 派生视图。"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord
from aiive.memory.memory_policy import MemoryPolicyEngine, MemoryReadChannel
from aiive.memory.memory_types import LifecycleState, ValidityState


class MemoryProjection:
    """将同一批 active 记忆快照导出为人类和机器可读格式。"""

    def __init__(self, db: Session):
        self._db: Session = db

    def _load_records(self) -> list[MemoryRecord]:
        """一次性读取确定排序的可见记录，保证两种格式来自同一快照。"""
        records = (
            self._db.query(MemoryRecord)
            .filter(
                MemoryRecord.lifecycle_state == LifecycleState.ACTIVE.value,
                MemoryRecord.validity_state == ValidityState.VALID.value,
            )
            .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id.asc())
            .limit(100)
            .all()
        )
        if not records:
            return []
        from aiive.forget.visibility_service import ForgetVisibilityService

        visible_ids = set(ForgetVisibilityService.filter_memory_ids(
            self._db, [record.id for record in records],
        ))
        return [record for record in records if record.id in visible_ids]

    @staticmethod
    def _render_json(records: list[MemoryRecord]) -> list[dict[str, Any]]:
        """将记录渲染为遵循 API 敏感度策略的 JSON 数据。"""
        policy = MemoryPolicyEngine()
        return [
            {
                "id": record.id,
                "memory_type": record.memory_type,
                "canonical_key": record.canonical_key,
                "scope_type": record.scope_type,
                "scope_id": record.scope_id,
                "content": policy.render_content(
                    record.content, record.sensitivity, MemoryReadChannel.API,
                ),
                "sensitivity": record.sensitivity or "normal",
                "confidence": record.confidence,
                "importance": record.importance,
                "lifecycle_state": record.lifecycle_state,
                "validity_state": record.validity_state,
                "record_version": record.record_version,
            }
            for record in records
        ]

    @staticmethod
    def _render_markdown(records: list[MemoryRecord]) -> str:
        """将记录渲染为遵循 API 敏感度策略的 Markdown。"""
        policy = MemoryPolicyEngine()
        lines = ["# AIive Active Memories", "", f"Records: {len(records)}", ""]
        for record in records:
            content = policy.render_content(
                record.content, record.sensitivity, MemoryReadChannel.API,
            )
            key_info = f" ({record.canonical_key})" if record.canonical_key else ""
            lines.append(
                f"- [{record.memory_type}] {content}"
                + f" (confidence: {record.confidence:.2f}, importance: {record.importance:.2f})"
                + key_info
            )
        return "\n".join(lines)

    def to_markdown(self) -> str:
        """实时导出 active 记忆 Markdown。"""
        return self._render_markdown(self._load_records())

    def to_json(self) -> list[dict[str, Any]]:
        """实时导出 active 记忆 JSON。"""
        return self._render_json(self._load_records())

    @staticmethod
    def _atomic_replace(target: Path, content: str) -> None:
        """在目标目录写临时文件并原子替换，避免读到部分内容。"""
        data = content.encode("utf-8")
        if target.exists() and target.read_bytes() == data:
            return
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    def write_projection(self, output_dir: Path) -> dict[str, Any]:
        """从同一数据库快照原子覆盖 Markdown 和 JSON 投影文件。"""
        resolved_dir = output_dir.expanduser().resolve(strict=False)
        if resolved_dir.exists() and not resolved_dir.is_dir():
            raise ValueError("记忆文件投影路径必须是目录")
        resolved_dir.mkdir(parents=True, exist_ok=True)

        records = self._load_records()
        markdown = self._render_markdown(records)
        markdown_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        snapshot_id = uuid4().hex
        generated_at = datetime.now(timezone.utc).isoformat()
        json_document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at,
            "record_count": len(records),
            "markdown_sha256": markdown_hash,
            "records": self._render_json(records),
        }

        markdown_path = resolved_dir / "memories.md"
        json_path = resolved_dir / "memories.json"
        self._atomic_replace(markdown_path, markdown)
        self._atomic_replace(
            json_path,
            json.dumps(json_document, indent=2, ensure_ascii=False) + "\n",
        )
        return {
            "markdown": str(markdown_path),
            "json": str(json_path),
            "snapshot_id": snapshot_id,
            "record_count": len(records),
        }
