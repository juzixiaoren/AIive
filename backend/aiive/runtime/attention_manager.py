"""
运行时层 - 注意力管理器。

负责追踪和计算 Agent 的注意力状态（关注主题、注意力切换决策）。
当用户长时间不活跃或话题发生偏移时，自动建议注意力切换。
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AttentionState, Event

# 软切换阈值：4 小时后建议切换注意力
TOPIC_SWITCH_THRESHOLD_HOURS = 4
# 硬切换阈值：12 小时后强制切换注意力
HARD_SWITCH_THRESHOLD_HOURS = 12


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

    def recompute(self, thread_id: str, current_topic: str = "") -> dict[str, Any]:
        """重新计算注意力状态。

        根据最近事件的主题和当前注意力状态的创建时间，
        判断是否需要切换注意力，并生成新的 AttentionState 记录。

        Args:
            thread_id: 线程 ID
            current_topic: 当前主题（可选）

        Returns:
            包含 decision、suggestion、recent_topics、focus_topic 的字典
        """
        last_state = self.get_current(thread_id)
        now = datetime.now(timezone.utc)

        # 从事件表中获取最近的主题
        events = (
            self._db.query(Event)
            .filter(Event.thread_id == thread_id, Event.event_type == "user_message")
            .order_by(Event.created_at.desc())
            .limit(10)
            .all()
        )
        recent_topics = [e.payload.get("content", "")[:50] for e in events]

        # 决策逻辑：根据上一次注意力状态的持续时间判断
        decision = "continue"
        suggestion = None

        if last_state:
            created = last_state.created_at.replace(tzinfo=timezone.utc) if last_state.created_at.tzinfo is None else last_state.created_at
            hours = (now - created).total_seconds() / 3600

            if hours > HARD_SWITCH_THRESHOLD_HOURS:
                decision = "switch"
                suggestion = "new_focus_suggested"
            elif hours > TOPIC_SWITCH_THRESHOLD_HOURS:
                decision = "suspend"
                suggestion = "soft_switch"
            else:
                decision = "continue"

        state = AttentionState(
            thread_id=thread_id,
            focus_topic=current_topic or (last_state.focus_topic if last_state else None),
            recent_topics=recent_topics[:5],
            decision=decision,
            suggestion=suggestion,
        )
        self._db.add(state)
        self._db.flush()

        return {
            "thread_id": thread_id,
            "decision": decision,
            "suggestion": suggestion,
            "recent_topics": recent_topics[:5],
            "focus_topic": state.focus_topic,
        }
