"""
运行时层 - 事件日志记录器。

负责将 Agent 运行时事件（用户消息、LLM 响应、工具调用等）持久化写入 Event
和 LLMCall 表，供后续审计、追踪和分析使用。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from aiive.db.models import Event, LLMCall


class EventLogger:
    """事件日志记录器：封装 Event 和 LLMCall 的数据库写入操作。

    Attributes:
        _db: SQLAlchemy 数据库会话
    """
    def __init__(self, db: Session):
        self._db = db

    def log_event(
        self,
        trace_id: str,
        thread_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> Event:
        """记录一条通用运行时事件。

        Args:
            trace_id: 链路追踪 ID
            thread_id: 会话线程 ID
            event_type: 事件类型（如 user_message、llm_response、chat_ended 等）
            payload: 事件携带的可选数据

        Returns:
            已创建的 Event 对象
        """
        event = Event(
            id=str(uuid.uuid4()),
            trace_id=trace_id,
            thread_id=thread_id,
            event_type=event_type,
            payload=payload or {},
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(event)
        self._db.flush()
        return event

    def log_llm_call(
        self,
        trace_id: str,
        thread_id: str,
        model: str,
        latency_ms: float,
        input_preview: str | None = None,
        output_preview: str | None = None,
    ) -> LLMCall:
        """记录一次 LLM 调用，包含模型、延迟和输入输出预览。

        Args:
            trace_id: 链路追踪 ID
            thread_id: 会话线程 ID
            model: 使用的模型名称
            latency_ms: 调用延迟（毫秒）
            input_preview: 输入预览（可选）
            output_preview: 输出预览（可选）

        Returns:
            已创建的 LLMCall 对象
        """
        call = LLMCall(
            id=str(uuid.uuid4()),
            trace_id=trace_id,
            thread_id=thread_id,
            model=model,
            latency_ms=latency_ms,
            input_preview=input_preview,
            output_preview=output_preview,
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(call)
        self._db.flush()
        return call
