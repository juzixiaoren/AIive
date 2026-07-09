"""
运行时层 - 线程状态管理。

负责会话线程（Thread）的创建与查询，以及最近消息历史的获取，
为 Agent 提供对话上下文。
"""
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from aiive.db.models import Thread, Event


class ThreadState:
    """线程状态管理器：管理会话线程和消息历史。

    Attributes:
        _db: SQLAlchemy 数据库会话
    """
    def __init__(self, db: Session):
        self._db = db

    def get_or_create_thread(self, thread_id: str | None = None) -> Thread:
        """获取或创建会话线程。

        如果传入了 thread_id 且该线程存在，则返回已有线程；
        否则创建新线程。

        Args:
            thread_id: 线程 ID（可选）

        Returns:
            Thread 对象
        """
        if thread_id:
            thread = self._db.get(Thread, thread_id)
            if thread:
                return thread

        thread = Thread(id=str(uuid.uuid4()))
        self._db.add(thread)
        self._db.flush()
        return thread

    def get_recent_messages(self, thread_id: str, limit: int = 20) -> list[dict]:
        """获取线程的最近消息历史。

        从 Event 表中查询最近的 user_message 和 llm_response 事件，
        转换为角色-内容格式的列表。llm_response 中可能携带 action_cards。

        Args:
            thread_id: 线程 ID
            limit: 需要获取的消息数量（上限）

        Returns:
            包含 role、content、action_cards、event_id 字段的字典列表
        """
        events = (
            self._db.query(Event)
            .filter(
                Event.thread_id == thread_id,
                Event.event_type.in_(["user_message", "llm_response"]),
            )
            .order_by(Event.created_at.asc())
            .limit(limit)
            .all()
        )

        messages: list[dict] = []
        for event in events:
            role = "user" if event.event_type == "user_message" else "assistant"
            p = event.payload or {}
            content = p.get("content", "")
            if content:
                messages.append({
                    "role": role,
                    "content": content,
                    "action_cards": p.get("action_cards", []),
                    "event_id": event.id,
                    "trace_id": event.trace_id,
                })
        return messages
