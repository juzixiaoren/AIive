from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiive.api.routes_inspector import router
from aiive.db.base import get_db
from aiive.db.forget_models import ContentProvenanceRef
from aiive.db.models import ContextSnapshot, Event, MemoryRecord, Thread, TurnRecord
from aiive.forget.phase_a_shield import execute_phase_a_shield

RUN = "/api/context-runs/trace"
ITEM = f"{RUN}/items/tool_result:0"


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def _snapshot(db, snapshot_id, content, *, trace_id="trace", thread_id="thread", created_at=None):
    if db.get(Thread, thread_id) is None:
        db.add(Thread(id=thread_id))
        db.flush()
    if db.get(MemoryRecord, "m1") is None:
        db.add(Thread(id="memory-thread"))
        db.flush()
        db.add(Event(id="memory-event", trace_id="memory-trace", thread_id="memory-thread", event_type="user_message"))
        db.flush()
        db.add(MemoryRecord(id="m1", memory_type="user_profile", content="test memory", source_event_id="memory-event"))
        db.add(ContentProvenanceRef(owner_type="memory_record", owner_id="m1", source_type="event", source_id="memory-event"))
    row = ContextSnapshot(
        id=snapshot_id, trace_id=trace_id, thread_id=thread_id,
        stable_prefix_hash="prefix", token_total=15,
        created_at=created_at or datetime.now(timezone.utc),
        context_items=[
            {"item_id": "tool_result:0", "kind": "tool_result", "source": "tools",
             "trust_level": "trusted", "content_preview": "old...", "preview_length": 6,
             "token_estimate": 12, "full_content": "internal item field"},
            {"item_id": "message:1", "role": "user", "content": "首行\n2\n3\n4\n内联正文尾部"},
        ],
        meta={
            "full_contents": {"tool_result:0": content, "orphan": "internal orphan body"},
            "total_tool_calls": 1,
            "selection_trace": {"policy_version": "fixture", "full_contents": "internal trace field"},
            "injected_memory_ids": [{"memory_id": "m1", "canonical_key": "example", "content": "internal memory field"}],
            "internal": "internal metadata",
        },
    )
    db.add(row)
    db.commit()
    return row


def test_initial_rest_response_only_exposes_bounded_previews_and_public_metadata(db, client):
    text = "首行\r\n\r\n  三\r\n四\r\n最后一行正文"
    _snapshot(db, "snapshot", text)

    response = client.get(RUN)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "full_contents" not in response.text
    assert "full_content" not in response.text
    assert "internal" not in response.text
    assert "最后一行正文" not in response.text
    assert "内联正文尾部" not in response.text
    data = response.json()
    assert data["trace_id"] == "trace"
    snapshot, = data["snapshots"]
    assert (snapshot["snapshot_id"], snapshot["trace_id"]) == ("snapshot", "trace")
    assert (snapshot["stable_prefix_hash"], snapshot["token_total"], snapshot["total_tool_calls"]) == ("prefix", 15, 1)
    assert snapshot["selection_trace"]["policy_version"] == "fixture"
    assert snapshot["injected_memory_ids"][0]["canonical_key"] == "example"
    result, legacy = snapshot["context_items"]
    assert result == {
        "item_id": "tool_result:0", "kind": "tool_result", "source": "tools", "trust_level": "trusted",
        "content_preview": "首行\r\n\r\n  三\r\n四", "preview_length": len(text),
        "preview_truncated": True, "has_detail": True, "token_estimate": 12,
    }
    assert legacy["content_preview"] == "首行\n2\n3\n4"
    assert legacy["has_detail"] is False


def test_detail_is_bound_to_snapshot_trace_and_item(db, client):
    _snapshot(db, "older", "旧正文\n第二行😀", created_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    _snapshot(db, "newer", "新正文")
    _snapshot(db, "other", "other body", trace_id="another-trace", thread_id="another-thread")
    _snapshot(db, "empty", "")
    payload = client.get(RUN).json()
    assert [row["snapshot_id"] for row in payload["snapshots"]] == ["empty", "newer", "older"]

    for snapshot_id, text in [("older", "旧正文\n第二行😀"), ("newer", "新正文"), ("empty", "")]:
        response = client.get(ITEM, params={"snapshot_id": snapshot_id})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "snapshot_id": snapshot_id, "item_id": "tool_result:0", "full_content": text, "content_length": len(text),
        }

    assert client.get(ITEM).status_code == 422
    assert client.get("/api/context-runs/missing").status_code == 404
    for snapshot_id in ["other", "missing"]:
        response = client.get(ITEM, params={"snapshot_id": snapshot_id})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "context_snapshot_not_found"
    for item_id in ["orphan", "missing", "message:1"]:
        response = client.get(f"{RUN}/items/{item_id}", params={"snapshot_id": "older"})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "context_item_not_found"


@pytest.mark.parametrize("selector", ["thread", "time_range", "all_user_data", "event", "turn", "injected_memory", "source_memory"])
def test_forget_shield_blocks_both_endpoints_before_context_scrub(db, client, selector):
    row = _snapshot(db, "hidden", "待遗忘正文")
    row.turn_sequence = 1
    db.add(TurnRecord(id="turn-row", turn_id="turn", thread_id=row.thread_id, turn_sequence=1))
    db.add(Event(id="source-event", trace_id="", thread_id=row.thread_id, turn_id="turn", event_type="user_message"))
    if selector == "source_memory":
        row.meta = {**row.meta, "injected_memory_ids": [], "source_memory_ids": ["m1"]}
    db.commit()
    assert client.get(ITEM, params={"snapshot_id": "hidden"}).status_code == 200

    selectors = {
        "thread": {"thread_id": row.thread_id},
        "time_range": {"time_from": datetime.now(timezone.utc) - timedelta(days=1)},
        "all_user_data": {"all_user_data": True},
        "event": {"event_ids": ["source-event"]},
        "turn": {"turn_ids": ["turn-row"]},
        "injected_memory": {"memory_ids": ["m1"]},
        "source_memory": {"memory_ids": ["m1"]},
    }
    execute_phase_a_shield(db, mode="everywhere", **selectors[selector])
    db.commit()

    assert row.meta["full_contents"]["tool_result:0"] == "待遗忘正文"
    assert client.get(RUN).status_code == 404
    assert client.get(ITEM, params={"snapshot_id": row.id}).status_code == 404
    if selector == "thread":
        _snapshot(db, "other-thread", "另一线程", thread_id="other-thread")
        assert client.get(ITEM, params={"snapshot_id": "other-thread"}).status_code == 200
        assert [item["snapshot_id"] for item in client.get(RUN).json()["snapshots"]] == ["other-thread"]


def test_scrubbed_snapshot_cannot_return_old_content(db, client):
    row = _snapshot(db, "snapshot", "待清除正文")
    row.context_items = []
    row.meta = {}
    db.commit()

    assert client.get(RUN).json()["snapshots"][0]["context_items"] == []
    assert client.get(ITEM, params={"snapshot_id": row.id}).status_code == 404
