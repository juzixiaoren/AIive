"""
运行时层 - 注意力管理器。

负责追踪和计算 Agent 的注意力状态（关注主题、注意力切换决策）。
当用户长时间不活跃或话题发生偏移时，自动建议注意力切换。
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from aiive.db.models import AttentionState, Event

# 软切换阈值：4 小时后建议切换注意力
TOPIC_SWITCH_THRESHOLD_HOURS = 4
# 硬切换阈值：12 小时后强制切换注意力
HARD_SWITCH_THRESHOLD_HOURS = 12


def _as_utc(value: datetime) -> datetime:
    """将数据库时间归一化为 UTC aware datetime。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class AttentionManager:
    """注意力管理器：追踪当前关注主题并根据时间衰减和近期话题决定是否需要切换。

    Attributes:
        _db: SQLAlchemy 数据库会话
    """
    def __init__(self, db: Session):
        self._db: Session = db

    def get_current(self, thread_id: str) -> AttentionState | None:
        """获取指定线程的最新注意力状态。"""
        return (
            self._db.query(AttentionState)
            .filter(AttentionState.thread_id == thread_id)
            .order_by(AttentionState.created_at.desc())
            .first()
        )

    def resolve_for_turn(
        self,
        thread_id: str,
        current_turn_id: str,
        current_topic: str,
    ) -> dict[str, Any]:
        """计算本轮注意力，并仅在状态转换时持久化。"""
        current_topic = " ".join(current_topic.split())[:50]
        last_state = self.get_current(thread_id)
        previous_event = (
            self._db.query(Event)
            .filter(
                Event.thread_id == thread_id,
                Event.event_type == "user_message",
                # SQL 三值逻辑：`turn_id != x` 会排除 turn_id 为 NULL 的旧事件，
                # 需显式纳入 NULL。
                or_(Event.turn_id.is_(None), Event.turn_id != current_turn_id),
            )
            .order_by(Event.created_at.desc())
            .first()
        )
        recent_events = (
            self._db.query(Event)
            .filter(
                Event.thread_id == thread_id,
                Event.event_type == "user_message",
            )
            .order_by(Event.created_at.desc())
            .limit(5)
            .all()
        )
        recent_topics = [
            str((event.payload or {}).get("content", ""))[:50]
            for event in recent_events
            if (event.payload or {}).get("content")
        ]
        if current_topic and current_topic not in recent_topics:
            recent_topics.insert(0, current_topic)
        recent_topics = recent_topics[:5]

        decision = "continue"
        suggestion: str | None = None
        focus_topic = last_state.focus_topic if last_state else current_topic or None
        if previous_event is None:
            should_persist = last_state is None
        else:
            idle_hours = (
                datetime.now(timezone.utc) - _as_utc(previous_event.created_at)
            ).total_seconds() / 3600
            decision, suggestion = self._decision_for_idle(idle_hours)
            if decision == "switch":
                focus_topic = current_topic or focus_topic
            should_persist = (
                last_state is None
                or last_state.decision != decision
                or decision == "switch" and last_state.focus_topic != focus_topic
            )

        if should_persist:
            self._db.add(AttentionState(
                thread_id=thread_id,
                focus_topic=focus_topic,
                recent_topics=recent_topics,
                decision=decision,
                suggestion=suggestion,
            ))
            self._db.flush()

        return {
            "thread_id": thread_id,
            "decision": decision,
            "suggestion": suggestion,
            "recent_topics": recent_topics,
            "focus_topic": focus_topic,
        }

    @staticmethod
    def render_for_context(state: dict[str, Any]) -> str:
        """将本轮注意力状态渲染为有界系统上下文。"""
        focus_topic = str(state.get("focus_topic") or "").strip()[:50]
        decision = str(state.get("decision") or "continue")
        if not focus_topic and decision == "continue":
            return ""
        lines = ["## 注意力上下文"]
        if focus_topic:
            lines.append(f"- 当前关注：{focus_topic}")
        if decision == "suspend":
            lines.append("- 对话在较长间隔后恢复；继续先前工作前，请先确认当前目标。")
        elif decision == "switch":
            lines.append("- 对话在长时间间隔后恢复；以当前用户消息作为新的关注重点。")
        return "\n".join(lines) + "\n"

    def inspect(self, thread_id: str, exclude_turn_id: str = "") -> dict[str, Any]:
        """只读查询注意力状态，附带基于最近用户消息的空闲评估。

        不产生任何 AttentionState 写入。空闲评估与 resolve_for_turn 使用同一
        基准（最近一条用户消息时间）和同一阈值决策逻辑。

        Args:
            thread_id: 线程 ID
            exclude_turn_id: 计算空闲间隔时排除的回合（通常为当前回合，
                避免本轮刚落库的用户消息把空闲时长清零）

        Returns:
            包含持久状态（decision/suggestion/focus_topic/recent_topics）
            与空闲评估（idle_hours/projected_decision/projected_suggestion）的字典
        """
        state = self.get_current(thread_id)
        query = self._db.query(Event).filter(
            Event.thread_id == thread_id,
            Event.event_type == "user_message",
        )
        if exclude_turn_id:
            query = query.filter(
                or_(Event.turn_id.is_(None), Event.turn_id != exclude_turn_id),
            )
        last_event = query.order_by(Event.created_at.desc()).first()

        idle_hours: float | None = None
        projected_decision = "continue"
        projected_suggestion: str | None = None
        if last_event is not None:
            idle_hours = (
                datetime.now(timezone.utc) - _as_utc(last_event.created_at)
            ).total_seconds() / 3600
            projected_decision, projected_suggestion = self._decision_for_idle(idle_hours)

        return {
            "thread_id": thread_id,
            "decision": state.decision if state else "continue",
            "suggestion": state.suggestion if state else None,
            "focus_topic": state.focus_topic if state else None,
            "recent_topics": list(state.recent_topics or []) if state else [],
            "idle_hours": round(idle_hours, 2) if idle_hours is not None else None,
            "projected_decision": projected_decision,
            "projected_suggestion": projected_suggestion,
        }

    @staticmethod
    def _decision_for_idle(idle_hours: float) -> tuple[str, str | None]:
        """根据空闲小时数返回 (decision, suggestion)。"""
        if idle_hours > HARD_SWITCH_THRESHOLD_HOURS:
            return "switch", "new_focus_suggested"
        if idle_hours > TOPIC_SWITCH_THRESHOLD_HOURS:
            return "suspend", "soft_switch"
        return "continue", None
