"""注意力只读查询与噪声清理测试。

覆盖：
- AttentionManager.inspect 只读语义（不新增 AttentionState）；
- inspect 的空闲评估与 resolve_for_turn 同基准（最近用户消息，排除当前回合）；
- query_attention 工具默认当前线程、只读且不再污染 focus_topic；
- repair_attention_noise 清理脚本删除污染记录与连续重复记录。
"""
from datetime import datetime, timedelta, timezone

from aiive.context.run_context import RunContext
from aiive.db.models import AttentionState, Event, Thread
from aiive.runtime.attention_manager import AttentionManager


def _make_thread(db) -> Thread:
    thread = Thread(title="attention-test")
    db.add(thread)
    db.flush()
    return thread


def _add_user_message(db, thread_id: str, content: str, hours_ago: float, turn_id: str) -> Event:
    event = Event(
        trace_id="trace-attn",
        thread_id=thread_id,
        event_type="user_message",
        payload={"content": content},
        turn_id=turn_id,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )
    db.add(event)
    db.flush()
    return event


def _state_count(db) -> int:
    return db.query(AttentionState).count()


def test_inspect_empty_thread_returns_defaults_without_write(db):
    thread = _make_thread(db)
    result = AttentionManager(db).inspect(thread.id)
    assert result["decision"] == "continue"
    assert result["focus_topic"] is None
    assert result["idle_hours"] is None
    assert result["projected_decision"] == "continue"
    assert _state_count(db) == 0


def test_inspect_returns_persisted_state_and_idle_assessment(db):
    thread = _make_thread(db)
    _add_user_message(db, thread.id, "旧话题", hours_ago=5, turn_id="turn-1")
    db.add(AttentionState(
        thread_id=thread.id, focus_topic="旧话题",
        recent_topics=["旧话题"], decision="continue", suggestion=None,
    ))
    db.flush()

    before = _state_count(db)
    result = AttentionManager(db).inspect(thread.id)

    assert result["focus_topic"] == "旧话题"
    assert result["decision"] == "continue"
    assert result["idle_hours"] is not None and 4.9 <= result["idle_hours"] <= 5.1
    # 5 小时超过 4 小时软切换阈值
    assert result["projected_decision"] == "suspend"
    assert result["projected_suggestion"] == "soft_switch"
    assert _state_count(db) == before


def test_inspect_excludes_current_turn_for_idle(db):
    thread = _make_thread(db)
    _add_user_message(db, thread.id, "上一轮", hours_ago=13, turn_id="turn-old")
    _add_user_message(db, thread.id, "本轮消息", hours_ago=0, turn_id="turn-current")

    result = AttentionManager(db).inspect(thread.id, exclude_turn_id="turn-current")
    # 空闲基准应为上一轮消息（13 小时前），超过 12 小时硬切换阈值
    assert result["idle_hours"] is not None and result["idle_hours"] > 12
    assert result["projected_decision"] == "switch"

    result_no_exclude = AttentionManager(db).inspect(thread.id)
    assert result_no_exclude["idle_hours"] is not None
    assert result_no_exclude["idle_hours"] < 1


def test_query_attention_tool_is_readonly_and_defaults_to_ctx_thread(db):
    from aiive.tools.builtin_tools import _handle_query_attention

    thread = _make_thread(db)
    _add_user_message(db, thread.id, "话题A", hours_ago=1, turn_id="turn-1")
    db.add(AttentionState(
        thread_id=thread.id, focus_topic="话题A",
        recent_topics=["话题A"], decision="continue", suggestion=None,
    ))
    db.commit()

    ctx = RunContext(thread_id=thread.id, trace_id="trace-attn", turn_id="turn-2")
    result = _handle_query_attention(ctx=ctx)

    assert result["thread_id"] == thread.id
    assert result["focus_topic"] == "话题A"
    # 工具查询不得再写入 AttentionState，也不得把 "query" 写入焦点
    assert result["focus_topic"] != "query"
    assert db.query(AttentionState).count() == 1


def test_repair_attention_noise_removes_pollution_and_duplicates(db):
    from scripts.repair_attention_noise import repair

    thread = _make_thread(db)
    base = datetime.now(timezone.utc) - timedelta(hours=10)

    def _add_state(minutes: int, decision: str, focus: str | None, suggestion: str | None = None):
        db.add(AttentionState(
            thread_id=thread.id, focus_topic=focus,
            recent_topics=[], decision=decision, suggestion=suggestion,
            created_at=base + timedelta(minutes=minutes),
        ))

    _add_state(0, "continue", "话题A")           # 保留（首条）
    _add_state(10, "continue", "话题A")          # 删除（连续重复）
    _add_state(20, "continue", "query")          # 删除（污染）
    _add_state(30, "continue", "话题A")          # 删除（去污后与首条连续重复）
    _add_state(40, "suspend", "话题A", "soft_switch")  # 保留（状态转换）
    _add_state(50, "continue", "话题B")          # 保留（状态转换）
    db.commit()

    stats = repair(db, dry_run=False)
    assert stats["polluted_deleted"] == 1
    assert stats["duplicates_deleted"] == 2

    remaining = (
        db.query(AttentionState)
        .filter(AttentionState.thread_id == thread.id)
        .order_by(AttentionState.created_at)
        .all()
    )
    assert [(s.decision, s.focus_topic) for s in remaining] == [
        ("continue", "话题A"),
        ("suspend", "话题A"),
        ("continue", "话题B"),
    ]
