"""Real central Scrub/Verify, rollback, shared references and legacy compensation."""

import uuid
from datetime import datetime, timezone, timedelta
import pytest
from aiive.db.models import Attachment, Event
from aiive.db.forget_models import ContentProvenanceRef, ForgetAction
from aiive.forget.content_scope import load_scope
from aiive.forget.content_scrub import scrub_all, verify_scope, process_object_deletions
from aiive.runtime import attachments
from aiive.storage import object_store
from tests.integration.backend.test_forget_content_coverage import (
    _op,
    _event,
    _thread,
    _turn,
    _isolated_object_store,
)  # noqa: F401


@pytest.fixture
def db_session(db):
    return db


def seed(db, thread=None, bound=False):
    tid = thread or _thread(db)
    eid = _event(db, tid)
    turn = _turn(db, tid) if bound else None
    if turn:
        db.get(Event, eid).turn_id = turn.turn_id
    else:
        db.get(Event, eid).event_type = "attachment_uploaded"
    ref, digest = object_store.put_content_addressed(
        attachments.BUCKET, b"original raw attachment"
    )
    row = Attachment(
        id=str(uuid.uuid4()),
        thread_id=tid,
        event_id=eid,
        turn_record_id=turn.id if turn else None,
        filename="secret.txt",
        mime="text/plain",
        size=23,
        sha256=digest,
        object_key=ref.key,
    )
    db.add(row)
    db.flush()
    return row, ref


@pytest.mark.parametrize(
    "selector", ["event", "turn", "thread", "time_range", "all_user_data"]
)
def test_attachment_scrub_verify_and_post_commit_delete(db_session, selector):
    row, ref = seed(db_session, bound=selector == "turn")
    if selector == "event":
        kind, payload = "event_ids", {"event_ids": [row.event_id]}
    elif selector == "turn":
        kind, payload = "turn_ids", {"turn_ids": [row.turn_record_id]}
    elif selector == "thread":
        kind, payload = "thread", {"thread_id": row.thread_id}
    elif selector == "time_range":
        kind, payload = (
            selector,
            {"time_from": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
        )
    else:
        kind, payload = selector, {"all_user_data": True}
    op = _op(db_session, mode="everywhere", selector_type=kind, payload=payload)
    db_session.commit()
    scope = load_scope(db_session, op, "everywhere")
    scrub_all(db_session, scope)
    assert object_store.exists(ref)
    db_session.rollback()
    db_session.refresh(row)
    assert row.object_key and object_store.exists(ref)
    scrub_all(db_session, scope)
    db_session.commit()
    assert not verify_scope(db_session, scope).ok  # raw object action is still pending
    process_object_deletions(db_session, op)
    db_session.expire_all()
    assert verify_scope(db_session, scope).ok
    assert not object_store.exists(ref)
    assert not row.object_key and not row.sha256 and row.size == 0


def test_shared_object_and_memory_only(db_session):
    first, ref = seed(db_session)
    second, _ = seed(db_session)
    op = _op(
        db_session,
        mode="everywhere",
        selector_type="event_ids",
        payload={"event_ids": [first.event_id]},
    )
    db_session.commit()
    scope = load_scope(db_session, op, "everywhere")
    scrub_all(db_session, scope)
    db_session.commit()
    process_object_deletions(db_session, op)
    assert object_store.exists(ref)
    assert attachments.visible(db_session, second)
    op2 = _op(
        db_session,
        mode="memory_only",
        selector_type="event_ids",
        payload={"event_ids": [second.event_id]},
    )
    db_session.commit()
    scrub_all(db_session, load_scope(db_session, op2, "memory_only"))
    db_session.commit()
    assert second.object_key and object_store.exists(ref)


def test_legacy_pending_provenance_repair_and_forgotten_object_compensation(db_session):
    pending, ref = seed(db_session)
    actual, _ = seed(db_session, bound=True)
    original_source = actual.event_id
    db_session.add_all(
        [
            ContentProvenanceRef(
                owner_type="attachment_context",
                owner_id=actual.turn_record_id,
                source_type="event",
                source_id=pending.event_id,
            ),
            ContentProvenanceRef(
                owner_type="attachment_context",
                owner_id=actual.turn_record_id,
                source_type="event",
                source_id=original_source,
            ),
        ]
    )
    op = _op(
        db_session,
        mode="everywhere",
        selector_type="thread",
        payload={"thread_id": pending.thread_id},
    )
    db_session.commit()
    assert attachments.reconcile(db_session)["invalid_provenance"] == 1
    db_session.commit()
    assert attachments.reconcile(db_session)["invalid_provenance"] == 0
    db_session.commit()
    attachments.process_cleanup_actions(db_session)
    refs = (
        db_session.query(ContentProvenanceRef)
        .filter_by(owner_type="attachment_context")
        .all()
    )
    assert [r.source_id for r in refs] == [original_source]
    db_session.refresh(pending)
    assert not pending.object_key
    assert object_store.exists(
        ref
    )  # another visible attachment still owns the same bytes
    assert db_session.query(ForgetAction).filter_by(forget_operation_id=op).count() >= 1


def test_legacy_forgotten_original_is_compensated_and_verified(db_session):
    from aiive.db.forget_models import ForgetOperation
    row, ref = seed(db_session)
    db_session.get(Event, row.event_id).payload = {}
    op = _op(db_session, mode="everywhere", selector_type="event_ids", payload={"event_ids": [row.event_id]})
    db_session.get(ForgetOperation, op).status = "verified"
    db_session.commit()
    assert object_store.exists(ref)
    attachments.reconcile(db_session)
    db_session.commit()
    attachments.process_cleanup_actions(db_session)
    assert not object_store.exists(ref)
    assert verify_scope(db_session, load_scope(db_session, op, "everywhere")).ok
    count = db_session.query(ForgetAction).count()
    attachments.reconcile(db_session); db_session.commit()
    assert db_session.query(ForgetAction).count() == count
