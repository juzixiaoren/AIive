"""Attachment Forget uses real PostgreSQL commit/rollback and shared-reference checks."""

import pytest
from tests.integration.backend.test_chat_attachment_forget import (
    test_attachment_scrub_verify_and_post_commit_delete as _scrub_case,
    test_gc_operation_verified_for_retention as _gc_case,
    test_shared_object_and_memory_only as _shared_case,
    test_legacy_forgotten_original_is_compensated_and_verified as _compensate_case,
    test_legacy_pending_provenance_repair_and_forgotten_object_compensation as _repair_case,
)
from tests.integration.backend.test_forget_content_coverage import (
    _isolated_object_store,
)  # noqa: F401


@pytest.mark.parametrize(
    "selector", ["event", "turn", "thread", "time_range", "all_user_data"]
)
def test_pg_attachment_commit_and_rollback(pg_session, selector):
    _scrub_case(pg_session, selector)


def test_pg_attachment_shared_object(pg_session):
    _shared_case(pg_session)


def test_pg_attachment_historical_repair(pg_session):
    _repair_case(pg_session)


def test_pg_attachment_compensated_original(pg_session):
    _compensate_case(pg_session)


@pytest.mark.parametrize("kind", ["normal", "empty", "shared", "retry"])
def test_pg_gc_verified_for_retention(pg_session, monkeypatch, kind):
    _gc_case(pg_session, monkeypatch, kind)


@pytest.mark.parametrize("worker", [False, True])
def test_pg_vision_releases_forget_lock_and_rejects_race(
    pg_session, pg_engine, tmp_path, monkeypatch, worker
):
    import io
    import uuid
    from types import SimpleNamespace
    from PIL import Image
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    from aiive.config import settings
    from aiive.context.run_context import RunContext
    from aiive.core.llm_client import LLMClient
    from aiive.db.models import Attachment, Event, Thread, TurnRecord
    from aiive.forget.phase_a_shield import execute_phase_a_shield
    from aiive.storage import object_store
    from aiive.task_runtime.repository import TaskRepository
    from aiive.tools import attachment_tools

    monkeypatch.setattr(object_store, "ROOT", tmp_path / "vision-race")
    monkeypatch.setattr(settings, "aiive_vision_model", "offline-vision")
    factory = sessionmaker(pg_engine)
    monkeypatch.setattr(attachment_tools, "SessionLocal", factory)
    thread = Thread(id=str(uuid.uuid4()))
    pg_session.add(thread)
    pg_session.flush()
    turn = TurnRecord(
        thread_id=thread.id, turn_id=str(uuid.uuid4()), status="completed"
    )
    event = Event(
        thread_id=thread.id,
        trace_id=str(uuid.uuid4()),
        event_type="user_message",
        turn_id=turn.turn_id,
        payload={"content": "describe image"},
    )
    pg_session.add_all([turn, event])
    pg_session.flush()
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    ref, sha = object_store.put_content_addressed(
        "chat-attachments", image.getvalue(), {}
    )
    row = Attachment(
        thread_id=thread.id,
        event_id=event.id,
        turn_record_id=turn.id,
        filename="red.png",
        mime="image/png",
        size=len(image.getvalue()),
        sha256=sha,
        object_key=ref.key,
    )
    pg_session.add(row)
    pg_session.flush()
    aid, turn_id = row.id, turn.id
    ctx = RunContext(thread_id=thread.id, trace_id="pg-vision")
    if worker:
        task = TaskRepository(pg_session).create_task(
            thread_id=thread.id,
            goal="图片分析",
            task_brief={
                "initial_input": {
                    "skill_id": "document_processing",
                    "attachment_ids": [aid],
                }
            },
            enqueue=False,
        )
        ctx.task_id = task.id
    pg_session.commit()
    called = []

    def chat(self, messages, **kwargs):
        # A different physical connection must acquire the exclusive Forget lock
        # during the network boundary, rather than waiting on the reader's lock.
        with factory() as other:
            other.execute(text("SET LOCAL lock_timeout = '2s'"))
            execute_phase_a_shield(other, mode="everywhere", turn_ids=[turn_id])
            other.commit()
        called.append(True)
        return SimpleNamespace(content="must be discarded", model="offline-vision")

    monkeypatch.setattr(LLMClient, "chat", chat)
    with pytest.raises(ValueError, match="attachment_source_changed|task_unavailable"):
        attachment_tools.read_attachment(aid, ctx=ctx)
    assert called == [True]
