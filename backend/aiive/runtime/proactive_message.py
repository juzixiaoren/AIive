"""主动消息：把触发决策产出的文本写成聊天时间线里的真实助手消息。

与通知的区别：
- 通知（Event(notification_created)）只进角标与通知页；
- 主动消息写入 TurnRecord(source=runtime_event) + Event(llm_response)，
  与用户消息共享同一条时间线、同一个 turn_sequence 排序；
- 前台 Turn 正在执行时拒绝写入（ProactiveBlocked），由 Outbox 稍后重试，
  绝不在同一线程里并发插入导致乱序。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import Event, Thread, TurnRecord
from aiive.runtime.epoch_manager import EpochManager, peek_next_turn_sequence

MESSAGE_SOURCE = "runtime_event"
MAX_MESSAGE_CHARS = 4000


class ProactiveBlocked(RuntimeError):
    """线程存在活跃前台 Turn：本轮不写入，交给 Outbox 重试。"""


class ProactiveMessageService:
    def __init__(self, db: Session):
        self.db: Session = db

    def post(
        self,
        *,
        thread_id: str,
        content: str,
        trace_id: str,
        turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入一条用户可见的助手消息；重复调用同一 turn_id 返回已有事件。"""
        text = (content or "").strip()
        if not text:
            raise ValueError("empty_message")
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS]
        turn_id = turn_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{trace_id}:{text[:200]}"))
        now = datetime.now(timezone.utc)

        thread = self.db.query(Thread).filter(Thread.id == thread_id).with_for_update().one_or_none()
        if thread is None:
            raise ValueError("thread_not_found")

        existing = self.db.query(TurnRecord).filter(
            TurnRecord.thread_id == thread_id,
            TurnRecord.turn_id == turn_id,
        ).one_or_none()
        if existing is not None:
            event = self.db.query(Event).filter(
                Event.thread_id == thread_id,
                Event.turn_id == turn_id,
                Event.event_type == "llm_response",
            ).order_by(Event.turn_event_index.asc()).first()
            if event is not None:
                return {
                    "event_id": event.id,
                    "turn_id": turn_id,
                    "turn_sequence": existing.turn_sequence,
                    "created": False,
                }
            if existing.status == "running":
                raise ProactiveBlocked("turn_in_progress")

        active = self.db.query(TurnRecord).filter(
            TurnRecord.thread_id == thread_id,
            TurnRecord.status == "running",
            TurnRecord.lease_expires_at.is_not(None),
            TurnRecord.lease_expires_at > now,
        ).first()
        if active is not None:
            raise ProactiveBlocked("turn_in_progress")

        sequence = peek_next_turn_sequence(self.db, thread_id)
        epoch, segment = EpochManager().ensure_epoch_and_segment(self.db, thread_id, sequence)
        fingerprint = hashlib.sha256(
            f"{thread_id}:{turn_id}:{text}".encode("utf-8")
        ).hexdigest()
        record = TurnRecord(
            thread_id=thread_id,
            turn_id=turn_id,
            turn_sequence=sequence,
            status="completed",
            attempt_no=1,
            request_fingerprint=fingerprint,
            source=MESSAGE_SOURCE,
            epoch_id=epoch.id,
            segment_id=segment.id,
        )
        event = Event(
            id=str(uuid.uuid4()),
            trace_id=trace_id,
            thread_id=thread_id,
            event_type="llm_response",
            turn_id=turn_id,
            turn_event_index=0,
            payload={
                "content": text,
                "message_source": MESSAGE_SOURCE,
                "proactive": True,
                "trigger": dict(metadata or {}),
            },
        )
        self.db.add(record)
        self.db.add(event)
        thread.last_activity_at = now
        self.db.flush()
        return {
            "event_id": event.id,
            "turn_id": turn_id,
            "turn_sequence": sequence,
            "created": True,
        }
