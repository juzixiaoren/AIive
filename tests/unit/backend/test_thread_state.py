"""测试 ThreadState（线程状态管理）模块。

覆盖线程创建、获取历史消息、消息排序及消息数量限制等功能。
"""

from aiive.db.models import Event, TurnRecord
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.thread_state import ThreadState


class _TokenCount:
    """为有界历史读取测试提供确定性的 token 计数结果。"""

    def __init__(self, safe_tokens: int):
        self.safe_tokens = safe_tokens


class _TokenCounter:
    """按消息数量估算 token，避免测试依赖真实 tokenizer。"""

    def count_messages(self, _model, messages):
        return _TokenCount(max(1, len(messages)))


class TestThreadState:
    """测试 ThreadState 的线程和消息管理功能。"""

    def test_get_or_create_creates_new_thread(self, db_session):
        """不传 ID 时应创建新线程。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.flush()

        assert thread.id is not None
        assert thread.created_at is not None

    def test_get_or_create_returns_existing(self, db_session):
        """传入已存在 ID 时应返回已有线程。"""
        state = ThreadState(db_session)
        t1 = state.get_or_create_thread()
        db_session.flush()

        t2 = state.get_or_create_thread(thread_id=t1.id)
        assert t2.id == t1.id

    def test_get_or_create_new_when_not_found(self, db_session):
        """传入不存在的 ID 时应创建新线程（ID 被替换）。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread(thread_id="nonexistent-id")
        db_session.flush()

        assert thread.id != "nonexistent-id"

    def test_load_recent_messages_bounded_empty(self, db_session):
        """空线程应返回空消息列表。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.flush()

        messages, stats = state.load_recent_messages_bounded(
            db_session, thread.id, 100, _TokenCounter(), "test-model",
        )
        assert messages == []
        assert stats.turns_included == 0

    def test_load_recent_messages_bounded_returns_ordered(self, db_session):
        """有界读取应按 Turn 顺序返回消息，并跳过非上下文事件。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        for sequence, contents in enumerate((("Hi", "Hello!"), ("How are you?", "Good!")), start=1):
            turn_id = f"turn-{sequence}"
            db_session.add(TurnRecord(
                thread_id=thread.id, turn_id=turn_id, turn_sequence=sequence,
                status="completed", request_fingerprint=f"fp-{sequence}",
            ))
            db_session.add_all([
                Event(trace_id=f"trace-{sequence}", thread_id=thread.id, turn_id=turn_id,
                      turn_event_index=0, event_type="user_message", payload={"content": contents[0]}),
                Event(trace_id=f"trace-{sequence}", thread_id=thread.id, turn_id=turn_id,
                      turn_event_index=1, event_type="llm_response", payload={"content": contents[1]}),
                Event(trace_id=f"trace-{sequence}", thread_id=thread.id, turn_id=turn_id,
                      turn_event_index=2, event_type="chat_started", payload={}),
            ])
        db_session.flush()

        messages, stats = state.load_recent_messages_bounded(
            db_session, thread.id, 100, _TokenCounter(), "test-model",
        )
        assert [message["content"] for message in messages] == [
            "Hi", "Hello!", "How are you?", "Good!",
        ]
        assert [message["type"] for message in messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert stats.turns_included == 2

    def test_load_recent_messages_bounded_respects_max_turns(self, db_session):
        """有界读取应只保留最近的 max_turns 个完整 Turn。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        for sequence in range(1, 11):
            turn_id = f"turn-{sequence}"
            db_session.add(TurnRecord(
                thread_id=thread.id, turn_id=turn_id, turn_sequence=sequence,
                status="completed", request_fingerprint=f"fp-{sequence}",
            ))
            db_session.add(Event(
                trace_id=f"trace-{sequence}", thread_id=thread.id, turn_id=turn_id,
                turn_event_index=0, event_type="user_message", payload={"content": f"msg{sequence}"},
            ))
        db_session.flush()

        messages, stats = state.load_recent_messages_bounded(
            db_session, thread.id, 100, _TokenCounter(), "test-model", max_turns=5,
        )
        assert [message["content"] for message in messages] == [
            "msg6", "msg7", "msg8", "msg9", "msg10",
        ]
        assert stats.turns_included == 5

    def test_load_recent_messages_bounded_skips_empty_content(self, db_session):
        """有界读取应跳过空内容消息。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.add(TurnRecord(
            thread_id=thread.id, turn_id="turn-1", turn_sequence=1,
            status="completed", request_fingerprint="fp-1",
        ))
        db_session.add(Event(
            trace_id="trace-1", thread_id=thread.id, turn_id="turn-1",
            turn_event_index=0, event_type="user_message", payload={"content": ""},
        ))
        db_session.flush()

        messages, stats = state.load_recent_messages_bounded(
            db_session, thread.id, 100, _TokenCounter(), "test-model",
        )
        assert messages == []
        assert stats.turns_included == 1

    def test_list_thread_messages_page_preserves_structured_fields(self, db_session):
        """UI 历史页应恢复 trace、操作卡片和按 ID 聚合的工具记录。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.add(TurnRecord(
            thread_id=thread.id, turn_id="turn-1", turn_sequence=1,
            status="completed", request_fingerprint="fp-1",
        ))
        db_session.flush()
        db_session.add_all([
            Event(trace_id="trace-1", thread_id=thread.id, turn_id="turn-1", turn_event_index=0,
                  event_type="user_message", payload={"content": "执行任务"}),
            Event(trace_id="trace-1", thread_id=thread.id, turn_id="turn-1", turn_event_index=1,
                  event_type="tool_call", payload={"name": "demo", "params": {"x": 1}, "tool_call_id": "call-1"}),
            Event(trace_id="trace-1", thread_id=thread.id, turn_id="turn-1", turn_event_index=2,
                  event_type="tool_result", payload={"name": "demo", "result": {"ok": True}, "status": "completed", "tool_call_id": "call-1"}),
            Event(trace_id="trace-1", thread_id=thread.id, turn_id="turn-1", turn_event_index=3,
                  event_type="llm_response", payload={"content": "已完成", "action_cards": [{"trace_id": "trace-1"}]}),
        ])
        db_session.flush()

        page = state.list_thread_messages_page(thread.id, page_size=50)

        assert page["has_more"] is False
        assert [message["role"] for message in page["messages"]] == ["user", "assistant"]
        assistant = page["messages"][1]
        assert assistant["trace_id"] == "trace-1"
        assert assistant["action_cards"] == [{"trace_id": "trace-1"}]
        assert assistant["tool_calls"] == [{
            "tool_call_id": "call-1", "name": "demo", "params": {"x": 1},
            "status": "completed", "result": {"ok": True},
        }]

    def test_list_thread_messages_page_preserves_non_completed_tool_states(self, db_session):
        """失败、中断和在途 Turn 的工具事实必须原样进入 UI 历史。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        cases = (
            (1, "failed", "error", "failed"),
            (2, "interrupted_unknown", "execution_unknown", "execution_unknown"),
            (3, "running", "running", "execution_unknown"),
            (4, "not_started", None, "execution_unknown"),
        )
        for sequence, turn_status, result_status, expected_status in cases:
            turn_id = f"turn-state-{sequence}"
            db_session.add(TurnRecord(
                thread_id=thread.id,
                turn_id=turn_id,
                turn_sequence=sequence,
                status=turn_status,
                request_fingerprint=f"fp-state-{sequence}",
            ))
            db_session.add_all([
                Event(
                    trace_id=f"trace-state-{sequence}",
                    thread_id=thread.id,
                    turn_id=turn_id,
                    turn_event_index=0,
                    event_type="user_message",
                    payload={"content": f"状态{sequence}"},
                ),
                Event(
                    trace_id=f"trace-state-{sequence}",
                    thread_id=thread.id,
                    turn_id=turn_id,
                    turn_event_index=1,
                    event_type="tool_call",
                    payload={
                        "name": "demo",
                        "params": {"sequence": sequence},
                        "tool_call_id": f"call-state-{sequence}",
                    },
                ),
            ])
            if result_status is not None:
                db_session.add(Event(
                    trace_id=f"trace-state-{sequence}",
                    thread_id=thread.id,
                    turn_id=turn_id,
                    turn_event_index=2,
                    event_type="tool_result",
                    payload={
                        "name": "demo",
                        "result": {"sequence": sequence},
                        "status": result_status,
                        "tool_call_id": f"call-state-{sequence}",
                    },
                ))
        db_session.flush()

        page = state.list_thread_messages_page(thread.id, page_size=50)
        assistants = [message for message in page["messages"] if message["role"] == "assistant"]

        assert len(assistants) == 4
        assert [message["content"] for message in assistants] == ["", "", "", ""]
        assert [message["tool_calls"][0]["status"] for message in assistants] == [
            case[3] for case in cases
        ]
        assert [message["tool_calls"][0]["tool_call_id"] for message in assistants] == [
            f"call-state-{sequence}" for sequence in range(1, 5)
        ]

    def test_list_thread_messages_page_unknown_status_fails_closed(self, db_session):
        """历史未知工具状态必须显示待确认，不能默认完成。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        db_session.add(TurnRecord(
            thread_id=thread.id, turn_id="turn-unknown", turn_sequence=1,
            status="completed", request_fingerprint="fp-unknown",
        ))
        db_session.add_all([
            Event(trace_id="trace-unknown", thread_id=thread.id, turn_id="turn-unknown",
                  turn_event_index=0, event_type="tool_call",
                  payload={"name": "demo", "params": {}, "tool_call_id": "call-unknown"}),
            Event(trace_id="trace-unknown", thread_id=thread.id, turn_id="turn-unknown",
                  turn_event_index=1, event_type="tool_result",
                  payload={"name": "demo", "result": {}, "status": "legacy_mystery",
                           "tool_call_id": "call-unknown"}),
            Event(trace_id="trace-unknown", thread_id=thread.id, turn_id="turn-unknown",
                  turn_event_index=2, event_type="llm_response", payload={"content": "结果未知"}),
        ])
        db_session.flush()

        page = state.list_thread_messages_page(thread.id)

        assert page["messages"][0]["tool_calls"][0]["status"] == "execution_unknown"

    def test_list_thread_messages_page_uses_turn_cursor(self, db_session):
        """UI 历史页应使用 turn_sequence 游标向更早历史翻页。"""
        state = ThreadState(db_session)
        thread = state.get_or_create_thread()
        for sequence in range(1, 4):
            turn_id = f"turn-{sequence}"
            db_session.add(TurnRecord(
                thread_id=thread.id, turn_id=turn_id, turn_sequence=sequence,
                status="completed", request_fingerprint=f"fp-{sequence}",
            ))
            db_session.add(Event(
                trace_id=f"trace-{sequence}", thread_id=thread.id, turn_id=turn_id,
                turn_event_index=0, event_type="user_message", payload={"content": f"消息{sequence}"},
            ))
        db_session.flush()

        first = state.list_thread_messages_page(thread.id, page_size=2)
        second = state.list_thread_messages_page(
            thread.id, page_size=2, before_sequence=first["next_cursor"],
        )

        assert [message["content"] for message in first["messages"]] == ["消息2", "消息3"]
        assert first["next_cursor"] == 2
        assert first["has_more"] is True
        assert [message["content"] for message in second["messages"]] == ["消息1"]
        assert second["has_more"] is False
