"""历史记忆向量回填 bootstrap 回归测试。"""
from __future__ import annotations

from aiive.config import settings
from aiive.db.models import MemoryRecord, MemoryVectorProjection, OutboxJob
from aiive.memory.memory_types import LifecycleState, ValidityState
from aiive.memory.vector_bootstrap import _ensure_memory_vector_backfill_in_session
from aiive.memory.vector_projection import embedding_model_identity


def _memory(db_session, *, state: str = LifecycleState.ACTIVE.value) -> MemoryRecord:
    record = MemoryRecord(
        memory_type="user_profile",
        canonical_key="user.preference.theme",
        content="用户喜欢深色主题",
        scope_type="global",
        lifecycle_state=state,
        validity_state=ValidityState.VALID.value,
        record_version=1,
        content_hash="content-v1",
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_bootstrap_enqueues_missing_projection_once(db_session, monkeypatch):
    monkeypatch.setattr(settings, "aiive_embedding_provider", "local")
    monkeypatch.setattr(settings, "aiive_embedding_dimensions", 512)
    record = _memory(db_session)

    assert _ensure_memory_vector_backfill_in_session(db_session) == 1
    db_session.flush()
    assert _ensure_memory_vector_backfill_in_session(db_session) == 0

    job = db_session.query(OutboxJob).filter_by(
        job_type="memory_vector_refresh",
    ).one()
    assert job.payload["memory_id"] == record.id
    assert job.payload["record_version"] == 1


def test_bootstrap_skips_current_projection_and_inactive_memory(
    db_session, monkeypatch,
):
    monkeypatch.setattr(settings, "aiive_embedding_provider", "local")
    current = _memory(db_session)
    _memory(db_session, state=LifecycleState.SLEEPING.value)
    db_session.add(MemoryVectorProjection(
        memory_id=current.id,
        record_version=current.record_version,
        content_hash=current.content_hash,
        embedding_model=embedding_model_identity(),
        embedding=[0.01] * 512,
    ))
    db_session.flush()

    assert _ensure_memory_vector_backfill_in_session(db_session) == 0
