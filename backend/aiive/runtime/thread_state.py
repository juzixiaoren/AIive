"""
运行时层 - 线程状态管理。

负责会话线程（Thread）的创建与查询，以及最近消息历史的获取，
为 Agent 提供对话上下文。
"""
import uuid
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import Thread, Event


class ThreadState:
    """线程状态管理器：管理会话线程和消息历史。

    Attributes:
        _db: SQLAlchemy 数据库会话
    """
    def __init__(self, db: Session):
        self._db: Session = db

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

    def get_recent_messages(self, thread_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """获取线程的最近消息历史（含工具交互）。

        查询 user_message、tool_call、tool_result、llm_response 事件，
        按时间顺序返回结构化记录，用于重建完整的 LangChain 消息序列。
        tool_call 与 tool_result 按时间顺序成对出现。

        Args:
            thread_id: 线程 ID
            limit: 需要获取的事件数量（上限，默认 100）

        Returns:
            包含 type、content、tool_name、tool_params、tool_result 等字段的字典列表
        """
        events = (
            self._db.query(Event)
            .filter(
                Event.thread_id == thread_id,
                Event.event_type.in_([
                    "user_message", "tool_call", "tool_result", "llm_response",
                ]),
            )
            .order_by(Event.created_at.asc())
            .limit(limit)
            .all()
        )

        messages: list[dict[str, Any]] = []
        for event in events:
            p = event.payload or {}
            etype = event.event_type

            if etype == "user_message":
                content = p.get("content", "")
                if content:
                    messages.append({
                        "type": "user",
                        "content": content,
                        "event_id": event.id,
                        "trace_id": event.trace_id,
                    })

            elif etype == "tool_call":
                messages.append({
                    "type": "tool_call",
                    "tool_name": p.get("name", ""),
                    "tool_params": p.get("params", {}),
                    "event_id": event.id,
                    "trace_id": event.trace_id,
                })

            elif etype == "tool_result":
                messages.append({
                    "type": "tool_result",
                    "tool_name": p.get("name", ""),
                    "tool_result": p.get("result", {}),
                    "tool_status": p.get("status", "unknown"),
                    "event_id": event.id,
                    "trace_id": event.trace_id,
                })

            elif etype == "llm_response":
                content = p.get("content", "")
                if content:
                    messages.append({
                        "type": "assistant",
                        "content": content,
                        "action_cards": p.get("action_cards", []),
                        "event_id": event.id,
                        "trace_id": event.trace_id,
                    })

        return messages
