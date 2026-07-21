"""记忆 pgvector 投影服务的定向回归测试。"""
from __future__ import annotations

from aiive.config import settings
from aiive.db.models import MemoryRecord, MemoryVectorProjection
from aiive.memory.memory_types import LifecycleState, ValidityState
from aiive.memory.vector_projection import MemoryVectorProjectionService


class StubEmbeddingProvider:
    """单元测试专用 provider；不会进入生产装配路径。"""

    dimensions = 1536

    @staticmethod
    def embed(text: str) -> list[float]:
        assert text.strip()
        return [0.01] * 1536


def _record(db_session) -> MemoryRecord:
    record = MemoryRecord(
        memory_type="user_profile",
        canonical_key="user.preference.editor",
        content="用户喜欢深色编辑器",
        scope_type="global",
        lifecycle_state=LifecycleState.ACTIVE.value,
        validity_state=ValidityState.VALID.value,
        record_version=1,
        content_hash="hash-v1",
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_refresh_upserts_versioned_projection(db_session, monkeypatch):
    monkeypatch.setattr(settings, "aiive_embedding_model", "text-embedding-3-small")
    record = _record(db_session)
    service = MemoryVectorProjectionService(
        db_session, provider_factory=StubEmbeddingProvider,
    )

    assert service.refresh(record.id, 1) == "upserted"
    projection = db_session.get(MemoryVectorProjection, record.id)
    assert projection is not None
    assert projection.record_version == 1
    assert projection.content_hash == "hash-v1"
    assert len(projection.embedding) == 1536


def test_refresh_skips_stale_version(db_session):
    record = _record(db_session)
    service = MemoryVectorProjectionService(
        db_session, provider_factory=StubEmbeddingProvider,
    )

    assert service.refresh(record.id, 0) == "stale_version_skipped"
    assert db_session.get(MemoryVectorProjection, record.id) is None


def test_refresh_deletes_projection_after_lifecycle_change(db_session):
    record = _record(db_session)
    service = MemoryVectorProjectionService(
        db_session, provider_factory=StubEmbeddingProvider,
    )
    assert service.refresh(record.id, 1) == "upserted"

    record.lifecycle_state = LifecycleState.FORGOTTEN.value
    record.record_version = 2
    db_session.flush()

    assert service.refresh(record.id, 2) == "inactive_deleted"
    assert db_session.get(MemoryVectorProjection, record.id) is None
