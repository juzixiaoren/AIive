"""知识文档本地路径的统一最小权限边界。"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from sqlalchemy.orm import Session

from aiive.config import settings

from aiive.storage.data_paths import data_path


def allowed_knowledge_roots() -> tuple[Path, ...]:
    # 经 Settings 读取，确保写在 .env（而非仅 export 到进程环境）的配置生效。
    raw = settings.aiive_knowledge_roots
    roots = tuple(
        Path(part.strip()).expanduser().resolve()
        for part in raw.split(os.pathsep)
        if part.strip()
    )
    if roots:
        return roots
    # 开箱即用地支持用户文档，同时保留应用自己的导入投递目录。
    return (
        (Path.home() / "Documents").resolve(),
        data_path("knowledge"),
    )


def resolve_knowledge_path(file_path: str) -> Path:
    roots = allowed_knowledge_roots()
    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    allowed = os.pathsep.join(str(root) for root in roots)
    raise ValueError(f"knowledge_path_outside_allowed_roots:{allowed}")


def document_sources(db: Session, document_id: str) -> dict[str, list[str]]:
    from aiive.db.forget_models import ContentProvenanceRef

    grouped: dict[str, list[str]] = defaultdict(list)
    for row in db.query(ContentProvenanceRef).filter_by(owner_type="knowledge_document", owner_id=document_id):
        grouped[row.source_type].append(row.source_id)
    return dict(grouped)


def sources_payload(sources: dict[str, list[str]]) -> dict[str, list[str]]:
    aliases = {"event": "source_event_ids", "turn_record": "source_turn_id", "memory_record": "memory_ids"}
    return {aliases[kind]: ids for kind, ids in sources.items() if kind in aliases}
