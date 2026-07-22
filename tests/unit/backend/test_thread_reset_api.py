"""测试 /api/thread/reset 与 /api/tools/safe-delete 的外键健壮性。

覆盖前端传入未持久化 thread_id 时，接口应幂等成功而非触发外键违规。
"""
import pytest
from fastapi.testclient import TestClient

from aiive.db.base import get_db
from aiive.db.models import Thread, Event, TurnRecord
from aiive.main import create_app


@pytest.fixture
def client(db_session, monkeypatch):
    """创建测试用 HTTP 客户端，DB 依赖指向测试 SQLite 会话。"""
    from sqlalchemy.orm import sessionmaker as _sm
    from aiive.db import base

    test_factory = _sm(bind=db_session.get_bind())
    monkeypatch.setattr(base, "SessionLocal", test_factory)

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


class TestThreadResetAPI:
    """测试 POST /api/thread/reset 的外键健壮性。"""

    def test_reset_without_thread_id_ok(self, client):
        """不传 thread_id 时应直接返回成功。"""
        resp = client.post("/api/thread/reset", json={})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_reset_unpersisted_thread_ok(self, client, db_session):
        """传入未落库的 thread_id 时应幂等成功，且不写入事件。"""
        resp = client.post(
            "/api/thread/reset",
            json={"thread_id": "88ebe771-56f3-4a5a-9f82-ad1d0758c490"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        events = (
            db_session.query(Event)
            .filter(Event.event_type == "context_reset")
            .all()
        )
        assert events == []

    def test_reset_persisted_thread_logs_event(self, client, db_session):
        """传入已落库的 thread_id 时应记录 context_reset 事件。"""
        thread = Thread(id="thread-reset-1")
        db_session.add(thread)
        db_session.flush()

        resp = client.post(
            "/api/thread/reset",
            json={"thread_id": "thread-reset-1"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        events = (
            db_session.query(Event)
            .filter(
                Event.thread_id == "thread-reset-1",
                Event.event_type == "context_reset",
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].payload == {"reason": "user_requested"}


class TestThreadMessagesAPI:
    """测试 UI 历史分页接口的稳定响应契约。"""

    def test_messages_returns_trace_cards_and_tools(self, client, db_session):
        """历史接口应返回完整消息字段而不是内部 Event 字典。"""
        thread = Thread(id="thread-history-1")
        db_session.add(thread)
        db_session.add(TurnRecord(
            thread_id=thread.id, turn_id="turn-history-1", turn_sequence=1,
            status="completed", request_fingerprint="fp-history-1",
        ))
        db_session.flush()
        db_session.add_all([
            Event(trace_id="trace-history-1", thread_id=thread.id, turn_id="turn-history-1",
                  turn_event_index=0, event_type="user_message", payload={"content": "你好"}),
            Event(trace_id="trace-history-1", thread_id=thread.id, turn_id="turn-history-1",
                  turn_event_index=1, event_type="tool_call",
                  payload={"name": "demo", "params": {}, "tool_call_id": "call-history-1"}),
            Event(trace_id="trace-history-1", thread_id=thread.id, turn_id="turn-history-1",
                  turn_event_index=2, event_type="tool_result",
                  payload={"name": "demo", "result": "ok", "status": "completed", "tool_call_id": "call-history-1"}),
            Event(trace_id="trace-history-1", thread_id=thread.id, turn_id="turn-history-1",
                  turn_event_index=3, event_type="llm_response",
                  payload={"content": "完成", "action_cards": [{"trace_id": "trace-history-1"}]}),
        ])
        db_session.flush()

        response = client.get(f"/api/threads/{thread.id}/messages?page_size=50")

        assert response.status_code == 200
        data = response.json()
        assert data["has_more"] is False
        assert data["messages"][1]["trace_id"] == "trace-history-1"
        assert data["messages"][1]["action_cards"] == [{"trace_id": "trace-history-1"}]
        assert data["messages"][1]["tool_calls"][0]["tool_call_id"] == "call-history-1"

    def test_messages_returns_interrupted_tool_facts_without_llm_response(
        self, client, db_session,
    ):
        """中断 Turn 即使没有模型回复，也应返回真实的待确认工具卡片。"""
        thread = Thread(id="thread-history-interrupted")
        db_session.add(thread)
        db_session.add(TurnRecord(
            thread_id=thread.id,
            turn_id="turn-history-interrupted",
            turn_sequence=1,
            status="interrupted_unknown",
            request_fingerprint="fp-history-interrupted",
        ))
        db_session.flush()
        db_session.add_all([
            Event(
                trace_id="trace-history-interrupted",
                thread_id=thread.id,
                turn_id="turn-history-interrupted",
                turn_event_index=0,
                event_type="user_message",
                payload={"content": "执行未确认任务"},
            ),
            Event(
                trace_id="trace-history-interrupted",
                thread_id=thread.id,
                turn_id="turn-history-interrupted",
                turn_event_index=1,
                event_type="tool_call",
                payload={"name": "demo", "params": {}, "tool_call_id": "call-interrupted"},
            ),
            Event(
                trace_id="trace-history-interrupted",
                thread_id=thread.id,
                turn_id="turn-history-interrupted",
                turn_event_index=2,
                event_type="tool_result",
                payload={
                    "name": "demo",
                    "result": {"ok": False},
                    "status": "running",
                    "tool_call_id": "call-interrupted",
                },
            ),
        ])
        db_session.flush()

        response = client.get(f"/api/threads/{thread.id}/messages?page_size=50")

        assert response.status_code == 200
        messages = response.json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[1]["content"] == ""
        assert messages[1]["trace_id"] == "trace-history-interrupted"
        assert messages[1]["tool_calls"][0]["status"] == "execution_unknown"
        assert messages[1]["tool_calls"][0]["tool_call_id"] == "call-interrupted"


class TestSafeDeleteAPIThreadFK:
    """测试 POST /api/tools/safe-delete 的事件记录外键健壮性。"""

    def test_safe_delete_unpersisted_thread_ok(self, client, db_session):
        """传入未落库的 thread_id 时删除主流程不应因事件记录而失败。"""
        resp = client.post(
            "/api/tools/safe-delete",
            json={
                "path": "/tmp/does-not-exist",
                "scope_id": "workspace",
                "mode": "trash",
                "trace_id": "trace-x",
                "thread_id": "unpersisted-thread",
            },
        )
        assert resp.status_code == 200

        events = (
            db_session.query(Event)
            .filter(Event.event_type == "delete_request")
            .all()
        )
        assert events == []
